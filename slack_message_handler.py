"""
Enhanced Slack message handler with reaction logging support.

This extends the original message handler to include reaction_added event handling
that logs complete threads to files in the 'logs' directory for human review.
"""

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

def remove_slack_mentions(text: str) -> str:
    """
    Removes substrings like <@U1A2B3C4> from the input text.
    The pattern matches <@ followed by uppercase letters or digits, then >.

    Example: "Hello <@U068MGY0KSN>" -> "Hello "
    """
    pattern = r'<@[A-Z0-9]+>'
    return re.sub(pattern, "", text)

def markdown_to_slack(text: str) -> str:
    """Convert markdown formatting to Slack-compatible formatting."""
    # Convert double asterisks to single for bold
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)

    # Flatten nested bullets (replace ' *' with '• ')
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r' {4,}[*-] ', ' - ', text)

    # Convert Markdown link [text](url) to Slack's <url|text>
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)

    # Remove excessive newlines (keep one for paragraphs)
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

    async def replace_mentions_with_emails(self, text: str, client: AsyncWebClient) -> str:
        """
        Asynchronously replaces all Slack user mention patterns like <@USERID>
        with their corresponding user emails retrieved via self._get_user_email.

        Args:
            text: input string possibly with Slack mentions
            client: AsyncWebClient used for API calls

        Returns:
            string with all <@USERID> replaced by their emails
        """
        pattern = r'<@([A-Z0-9]+)>'  # capture USERID inside mentions
        matches = list(re.finditer(pattern, text))

        if not matches:
            return text

        parts = []
        last_index = 0
        # For each mention, get email and accumulate the string parts
        for m in matches:
            start, end = m.span()
            user_id = m.group(1)

            # Add the part before this mention
            parts.append(text[last_index:start])

            # Await email for this user_id
            email = await self._get_user_email(user_id, client)

            # Append the email
            parts.append(email)

            last_index = end

        # Append remaining part of the string
        parts.append(text[last_index:])
        return "".join(parts)
    async def _get_user_email(self, user_id: str, client: AsyncWebClient) -> str:
        """
        Fetch the email address of a Slack user from user_id using users.info.

        Requires 'users:read.email' bot token scope.

        Returns the email if found, else falls back to user_id.
        """
        try:
            response = await client.users_info(user=user_id)
            if response["ok"]:
                email = response.get("user", {}).get("profile", {}).get("email")
                if email:
                    return email
                if response.get("user",{}).get("is_bot"):
                    name = response.get("user", {}).get("real_name")
                    return name
            self.logger.warning(f"Email not found for user {user_id}")
        except SlackApiError as e:
            self.logger.warning(f"Failed to get email for user {user_id}: {e}")
        return user_id

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
            raw_user_id = event.get("user")
            if not text or not raw_user_id:
                return

            # Replace user_id with email fetched from Slack API
            user_id = await self._get_user_email(raw_user_id, client)

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
            context = await self._get_thread_context(client, channel, thread_ts, raw_user_id)
            # if not context:
            #     await say(
            #         text="❌ Could not retrieve conversation context.",
            #         thread_ts=thread_ts,
            #     )
            #     return

            # Track session
            # self.session_mgr.get_or_create_session(channel, thread_ts)
            # self.session_mgr.update_last_used(channel, thread_ts)

            # Stream response from Agent Engine
            # If it is an IM, send the real user_id and channel, send only text
            # If it is not an IM, send the thread_ts as the user_id, send whole context
            text = "<@" + user_id + ">: " + await self.replace_mentions_with_emails(text, client)
            if channel_type == "im":
                await self._stream_agent_response(text, user_id, thread_ts, say, channel)
            else:
                if context:
                    message = "Additional Thread Context:\n```\n" + context + "\n```\nUser Message:\n\n" + text
                    await self._stream_agent_response(message, thread_ts, thread_ts, say, channel)
                else:
                    await self._stream_agent_response(text, thread_ts, thread_ts, say, channel)

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

    async def _get_thread_context(self, client: AsyncWebClient, channel: str, thread_ts: str, user_id: str) -> str:
        """
        Fetch all messages in thread for context.

        Args:
            client: Slack web client
            channel: Channel ID
            thread_ts: Thread timestamp
            user_id: User ID

        Returns:
            Formatted thread context string
        """
        try:
            history = await client.conversations_replies(channel=channel, ts=thread_ts)
            messages = []

            for msg in history.get("messages", []):
                text = msg.get("text", "").strip()
                user = msg.get("user")
                if 'bot_id' in msg:
                    continue
                if user == user_id:
                    continue
                if text and user and msg.get("subtype") != "bot_message":
                    user_email = await self._get_user_email(user, client)
                    messages.append(f"<@{user_email}>: {remove_slack_mentions(text)}\n")

            return "\n".join(messages)

        except SlackApiError as e:
            self.logger.error(f"Error fetching thread context: {e}")
            return ""

    async def _stream_agent_response(self, context: str, user_id: str, thread_ts: str, say: AsyncSay, channel_id: str):
        """
        Stream response from Agent Engine and send final reply.

        Args:
            context: Message context to send to agent
            user_id: User ID who sent the message
            thread_ts: Thread timestamp for reply
            say: AsyncSay callable for sending response
        """
        response_chunks = []

        try:
            async for chunk in self.agent_client.stream_query(user_id=user_id, message=context, channel_id=channel_id):
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