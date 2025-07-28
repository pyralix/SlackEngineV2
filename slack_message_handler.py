"""
Enhanced Slack message handler with reaction logging support.

This extends the original message handler to include reaction_added event handling
that logs complete threads to files in the 'logs' directory for human review.
"""
import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from gcs_tools import upload_json_to_gcs
from deduplication import deduplicate_event

import requests
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from slack_bolt.async_app import AsyncSay

from gemini_tools import analyze_log_vertexai_with_json, remove_friendly_response_field
from session_manager import SessionManager
from agent_engine_client import AgentEngineClient


def extract_last_user_message(context: str, user_id: str) -> str:
    """
    Extract the latest message text authored by the user from the conversation context.
    Assumes context is a sequence of lines like: <@USERID>: message

    Args:
        context (str): Full conversation history as one string.
        user_id (str): The Slack user ID whose message to extract.

    Returns:
        str: The most recent message text sent by the user, or "" if not found.
    """
    user_pattern = re.compile(rf'^<@{re.escape(user_id)}>\s*:\s*(.+)$', re.MULTILINE)
    matches = user_pattern.findall(context)
    return matches[-1].strip() if matches else ""

def markdown_to_slack(text: str) -> str:
    """
    Convert Markdown formatting to Slack-compatible formatting.

    - Converts bold (`**bold**`) to Slack's `*bold*`
    - Converts headings (`# Heading`) to bold lines for visual emphasis
    - Converts bullets and numbered lists to bullet points
    - Converts `[text](url)` links to Slack `<url|text>`
    - Removes excessive newlines

    Args:
        text (str): Input string in Markdown format.

    Returns:
        str: Slack-formatted message.
    """
    if not text:
        return ""

    # Convert Markdown headings (#, ##, ###) to bold, handles up to ######
    def heading_repl(match):
        content = match.group(2).strip()
        return f"\n*{content}*\n"
    text = re.sub(r'^(#{1,6})\s+(.*)', heading_repl, text, flags=re.MULTILINE)

    # Convert double asterisks to Slack's bold
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)

    # Flatten nested bullets (replace list starts with • )
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r' {4,}[*-] ', ' - ', text)

    # Convert Markdown links to Slack format
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)

    # Remove excessive newlines (keep a max of 2)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


class EnhancedSlackMessageHandler:
    """
    Enhanced Slack message handler with reaction logging.
    
    Handles incoming Slack messages and logs threads when reactions
    are added to bot messages for human review.
    """
    
    def __init__(self, session_mgr: SessionManager, agent_client: AgentEngineClient, bot_name: str):
        """
        Initialize the enhanced message handler.
        
        Args:
            session_mgr: Session manager instance
            agent_client: Agent engine client instance
            bot_name: Name of the bot for logging purposes
        """
        self.session_mgr = session_mgr
        self.agent_client = agent_client
        self.bot_name = bot_name
        self.logger = logging.getLogger(f"{__name__}.{bot_name}")
        self.bot_user_id: Optional[str] = None
        
        # Create logs directory if it doesn't exist
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(exist_ok=True)
        
        self.logger.info(f"Enhanced message handler initialized for {bot_name}")

    @deduplicate_event()
    async def handle_message(self, event: dict, say: AsyncSay, client: AsyncWebClient):
        """
        Handle Slack 'message' event with enhanced logic.
        
        Rules:
        1. If DM (channel_type == "im"), respond to every message in thread.
        2. If channel/group, respond ONLY if message mentions the bot.
        3. Always respond in thread.
        
        Args:
            event: Slack message event
            say: AsyncSay callable for responses
            client: Slack web client
        """
        try:
            # Cache bot user ID on first message
            if not self.bot_user_id:
                try:
                    auth_response = await client.auth_test()
                    self.bot_user_id = auth_response["user_id"]
                    self.logger.info(f"Bot user ID: {self.bot_user_id}")
                except SlackApiError as e:
                    self.logger.error(f"Cannot get bot user ID: {e}")
                    return
            
            # Ignore bot messages to prevent loops
            if event.get("subtype") == "bot_message":
                return
            
            text = event.get("text", "").strip()
            user_id = event.get("user")
            if not text or not user_id:
                return
            
            channel = event["channel"]
            thread_ts = event.get("thread_ts") or event["ts"]
            channel_type = event.get("channel_type", "")
            
            # Determine if we should respond
            should_respond = False
            if channel_type == "im":
                # DM: Always respond
                should_respond = True
            else:
                # Channel/group: Only respond if bot is mentioned
                should_respond = f"<@{self.bot_user_id}>" in text
            
            if not should_respond:
                return
            
            self.logger.info(f"Processing message from {user_id} in {channel}: {text[:100]}...")
            
            # Get thread context
            context = await self._get_thread_context(client, channel, thread_ts)
            if not context:
                await say(
                    text="❌ Could not retrieve conversation context.",
                    thread_ts=thread_ts,
                )
                return
            
            # Track session
            self.session_mgr.get_or_create_session(channel, thread_ts)
            self.session_mgr.update_last_used(channel, thread_ts)
            
            # Stream response from Agent Engine
            await self._stream_agent_response(context, user_id, thread_ts, say)
            
        except Exception as e:
            self.logger.exception("Error handling Slack message")
            try:
                await say(
                    text="⚠️ An error occurred while processing your message.",
                    thread_ts=event.get("thread_ts") or event.get("ts"),
                )
            except Exception:
                pass

    async def handle_reaction_added(self, event: dict, client: AsyncWebClient):
        """
        When a reaction is added to a bot-authored message (root or thread), log the entire thread.
        """
        try:
            # Get bot user ID if not cached
            if not self.bot_user_id:
                auth_response = await client.auth_test()
                self.bot_user_id = auth_response["user_id"]

            reacted_item = event.get("item", {})
            if reacted_item.get("type") != "message":
                return

            channel = reacted_item.get("channel")
            message_ts = reacted_item.get("ts")
            if not channel or not message_ts:
                return

            # Try to get the message via conversations_history (root) or conversations_replies (thread)
            original_message = None
            try:
                # Try history (may hit parent only)
                resp = await client.conversations_history(
                    channel=channel, oldest=message_ts, latest=message_ts, inclusive=True, limit=1
                )
                candidates = resp.get("messages", [])
                if candidates:
                    original_message = candidates[0]
                else:
                    # Try as thread (may be reply)
                    resp = await client.conversations_replies(channel=channel, ts=message_ts)
                    candidates = resp.get("messages", [])
                    # Look for a message with ts == message_ts
                    for m in candidates:
                        if m.get("ts") == message_ts:
                            original_message = m
                            break

                if not original_message:
                    self.logger.warning(f"Could not find message {message_ts} in channel {channel}")
                    return
            except SlackApiError as e:
                self.logger.error(f"Cannot fetch reacted message: {e}")
                return

            if original_message.get("user") != self.bot_user_id:
                # Not a bot message, ignore.
                return

            # Determine thread root
            thread_ts = original_message.get("thread_ts") or original_message.get("ts")

            # Log the WHOLE thread for this reaction
            await self._log_thread_for_review(client, channel, thread_ts, event)

        except Exception:
            self.logger.exception("Error handling reaction_added event")

    async def _get_thread_context(self, client: AsyncWebClient, channel: str, thread_ts: str) -> str:
        """
        Fetch all messages in thread for context.
        
        Args:
            client: Slack web client
            channel: Channel ID
            thread_ts: Thread timestamp
            
        Returns:
            Formatted thread context string
        """
        try:
            history = await client.conversations_replies(channel=channel, ts=thread_ts)
            messages = []
            
            for msg in history.get("messages", []):
                text = msg.get("text", "").strip()
                user = msg.get("user")
                if text and user and msg.get("subtype") != "bot_message":
                    messages.append(f"<@{user}>: {text}")
            
            return "\n".join(messages)
            
        except SlackApiError as e:
            self.logger.error(f"Error fetching thread context: {e}")
            return ""

    async def _stream_agent_response(self, context: str, user_id: str, thread_ts: str, say: 'AsyncSay'):
        """
        Stream response from Agent Engine and send final reply.

        Only send the quick "I'm working" response if the user message is longer than 80 characters.
        Generates it asynchronously using asyncio.to_thread to avoid blocking the event loop.
        :type user_id: str
        """
        MIN_QUICK_RESPONSE_LEN = 30  # You can make this configurable

        # Only trigger for long messages
        user_msg = extract_last_user_message(context, user_id)
        if len(user_msg) > MIN_QUICK_RESPONSE_LEN:
            try:
                from gemini_tools import quick_working_response

                # Run the blocking Gemini model call in a worker thread
                quick_msg = await asyncio.to_thread(quick_working_response, context, user_id)
                if quick_msg:
                    await say(text=quick_msg, thread_ts=thread_ts)
            except Exception as e:
                self.logger.error(f"Quick working response generation failed: {e}")
                # Optionally: fallback to a generic message
                try:
                    await say(text=f"<@{user_id}> I'm working on your request!", thread_ts=thread_ts)
                except Exception:
                    self.logger.error("Failed to send fallback working message.")

        # Proceed as before to stream the actual agent response
        response_chunks = []
        try:
            async for chunk in self.agent_client.stream_query(user_id=user_id, message=context):
                response_chunks.append(chunk)
        except Exception as e:
            self.logger.error(f"Agent Engine streaming error: {e}")
            await say(
                text="❌ Sorry, I'm having trouble connecting to the agent service.",
                thread_ts=thread_ts
            )
            return

        final_response = response_chunks[-1] if response_chunks else "🤷 I don't have a response for that."
        self.logger.info(f"Sending response to {user_id}: {final_response[:100]}...")
        await say(text=markdown_to_slack(final_response), thread_ts=thread_ts)

    async def _log_thread_for_review(self, client: AsyncWebClient, channel: str, message_ts: str, reaction_event: dict):
        """
        Log complete thread to file in 'logs' directory for human review.
        
        Args:
            client: Slack web client
            channel: Channel ID
            message_ts: Message timestamp
            reaction_event: The reaction_added event
        """
        try:
            # Determine thread timestamp (could be the message itself or its thread)
            thread_ts = message_ts
            
            # Get complete thread history
            history_response = await client.conversations_replies(
                channel=channel,
                ts=thread_ts
            )
            
            messages = history_response.get("messages", [])
            if not messages:
                self.logger.warning(f"No messages found for thread {thread_ts}")
                return
            
            # Build thread log data
            log_data = {
                "timestamp": datetime.utcnow().isoformat(),
                "bot_name": self.bot_name,
                "channel": channel,
                "channel_name": channel,
                "thread_ts": thread_ts,
                "reaction_event": {
                    "user": reaction_event.get("user"),
                    "reaction": reaction_event.get("reaction"),
                    "event_ts": reaction_event.get("event_ts")
                },
                "thread_messages": []
            }
            
            # Process each message in the thread
            for msg in messages:
                message_data = {
                    "ts": msg.get("ts"),
                    "user": msg.get("user"),
                    "text": msg.get("text", ""),
                    "subtype": msg.get("subtype"),
                    "is_bot": msg.get("subtype") == "bot_message" or msg.get("user") == self.bot_user_id
                }
                log_data["thread_messages"].append(message_data)
            
            # Generate filename with timestamp and thread ID
            timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"thread_{timestamp_str}_{channel}_{thread_ts.replace('.', '_')}.json"
            log_file_path = self.logs_dir / filename
            
            # Write to file
            with open(log_file_path, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"Logged thread with reaction to: {log_file_path}")
            self.logger.info(f"Sending to Gemini for analysis")
            reaction_response = analyze_log_vertexai_with_json(log_data,reaction_event.get("user"))

            try:
                # Extract the friendly message for user feedback from the Gemini output
                friendly_message = reaction_response.get("friendly_response_to_user")
                if friendly_message:
                    # Find the thread timestamp and react in that thread
                    await client.chat_postMessage(
                        channel=channel,
                        text=friendly_message,
                        thread_ts=thread_ts
                    )
                    self.logger.info(f"Posted friendly feedback message to thread {thread_ts}")
                else:
                    self.logger.warning(f"No 'friendly_response_to_user' generated for thread {thread_ts}")
            except Exception as feedback_exc:
                self.logger.error(f"Failed to post feedback message: {feedback_exc}")


            try:
                cleaned_log = remove_friendly_response_field(reaction_response)

                learning_filename = f"learning_{timestamp_str}_{channel}_{thread_ts.replace('.', '_')}.json"

                # Use the bot's name as the GCS "folder"
                # Example: bot_name comes from config.slack_bot.name (pass to EnhancedSlackMessageHandler at startup)
                gcs_folder = self.bot_name

                upload_json_to_gcs(cleaned_log, learning_filename, gcs_folder)

                self.logger.info(
                    f"Uploaded learning log for thread {thread_ts} to GCS at {gcs_folder}/{learning_filename}")
            except Exception as upload_exc:
                self.logger.error(f"Failed to upload learning log to GCS: {upload_exc}")

        except Exception as e:
            self.logger.error(f"Error logging thread for review: {e}")