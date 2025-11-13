"""
Enhanced Slack message handler with reaction logging and relay support.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Set

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from slack_bolt.async_app import AsyncSay

from gemini_tools import analyze_log_vertexai_with_json, remove_friendly_response_field
from session_manager import SessionManager
from agent_engine_client import AgentEngineClient
from thread_link_storage import ThreadLinkStorage
from deduplication import deduplicate_event
from gcs_tools import upload_json_to_gcs


def remove_bot_mention(text: str, bot_user_id: str) -> str:
    """Removes only the specific bot's mention from text."""
    pattern = f'<@{bot_user_id}>'
    return text.replace(pattern, "").strip()


def markdown_to_slack(text: str) -> str:
    """Convert markdown formatting to Slack-compatible formatting."""
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r' {4,}[*-] ', ' - ', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class EnhancedSlackMessageHandler:
    """
    Handles incoming Slack messages, logs threads for review, and relays messages
    from notification channels back to original user threads.
    """

    def __init__(
        self,
        session_mgr: SessionManager,
        agent_client: AgentEngineClient,
        bot_name: str,
        thread_linker: ThreadLinkStorage,
    ):
        self.session_mgr = session_mgr
        self.agent_client = agent_client
        self.bot_name = bot_name
        self.thread_linker = thread_linker
        self.logger = logging.getLogger(f"{__name__}.{bot_name}")
        self.bot_user_id: Optional[str] = None
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(exist_ok=True)
        self.logger.info(f"Enhanced message handler initialized for {bot_name}")

    async def _get_bot_user_id(self, client: AsyncWebClient):
        """Caches the bot's user ID."""
        if not self.bot_user_id:
            try:
                auth_response = await client.auth_test()
                self.bot_user_id = auth_response["user_id"]
                self.logger.info(f"Cached bot user ID: {self.bot_user_id}")
            except SlackApiError as e:
                self.logger.error(f"Cannot get bot user ID: {e}")
        return self.bot_user_id

    async def _generate_relay_intro(self) -> str:
        """Generates a varied, first-person intro for a relay message."""
        prompt = (
            "You are about to relay a message from a human support agent to a user. "
            "Write a very brief, friendly, one-sentence introduction. For example: 'I have an update from the team:' "
            "or 'Here's some more information from the support team:'"
        )
        intro = ""
        # Use a unique user_id for this self-contained task
        async for chunk in self.agent_client.stream_query(None, "relay_intro_generator", prompt):
            intro += chunk
        return intro.strip()

    async def _relay_support_message(self, event: dict, client: AsyncWebClient) -> bool:
        """
        Checks if a message is a command in a notification thread and relays it.
        Returns True if the message was relayed, False otherwise.
        """
        thread_ts = event.get("thread_ts")
        text = event.get("text", "")
        bot_user_id = await self._get_bot_user_id(client)

        if not thread_ts or f"<@{bot_user_id}>" not in text:
            return False

        link_info = self.thread_linker.get_link(notification_ts=thread_ts)
        if not link_info:
            return False

        self.logger.info(f"Relay command detected in notification thread {thread_ts}.")

        original_channel = link_info["original_channel_id"]
        original_thread_ts = link_info["original_thread_ts"]
        
        # Preserve other mentions, remove only the bot's mention
        support_message = remove_bot_mention(text, bot_user_id)

        # Generate a natural introduction
        intro = await self._generate_relay_intro()
        
        relay_text = f"{intro}\n\n> {support_message}"

        try:
            await client.chat_postMessage(channel=original_channel, thread_ts=original_thread_ts, text=relay_text)
            self.logger.info(f"Successfully relayed message to original thread {original_channel}/{original_thread_ts}")
            # await client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="white_check_mark")
        except SlackApiError as e:
            self.logger.error(f"Failed to relay message: {e}")
            await client.chat_postMessage(
                channel=event["channel"],
                thread_ts=thread_ts,
                text=f"Sorry, I failed to send that message. Error: {e.response['error']}"
            )
        
        return True

    @deduplicate_event()
    async def handle_message(self, event: dict, say: AsyncSay, client: AsyncWebClient):
        """Handle Slack 'message' event with enhanced logic."""
        try:
            # This now correctly handles all mentions, including app_mentions,
            # because we removed the redundant app_mention handler.
            if await self._relay_support_message(event, client):
                return

            bot_user_id = await self._get_bot_user_id(client)
            if not bot_user_id or event.get("subtype") == "bot_message":
                return

            text = event.get("text", "").strip()
            raw_user_id = event.get("user")
            if not text or not raw_user_id:
                return

            channel = event["channel"]
            channel_type = event.get("channel_type", "")
            
            # The `message` event covers DMs, mentions, and app_mentions.
            should_respond = (channel_type == "im") or (f"<@{bot_user_id}>" in text)
            if not should_respond:
                return

            user_id = await self._get_user_email(raw_user_id, client)
            thread_ts = event.get("thread_ts") or event["ts"]
            self.logger.info(f"Processing message from {user_id} in {channel}: {text[:100]}...")

            context = await self._get_thread_context(client, channel, thread_ts, raw_user_id)
            
            text_with_mentions = "<@" + user_id + ">: " + await self.replace_mentions_with_emails(text, client)
            
            if channel_type == "im":
                await self._stream_agent_response(text_with_mentions, user_id, thread_ts, say, channel)
            else:
                message = f"Additional Thread Context:\n```\n{context}\n```\nUser Message:\n\n{text_with_mentions}" if context else text_with_mentions
                await self._stream_agent_response(message, thread_ts, thread_ts, say, channel)

        except Exception as e:
            self.logger.exception("Error handling Slack message")
            try:
                await say(text="⚠️ An error occurred while processing your message.", thread_ts=event.get("thread_ts") or event.get("ts"))
            except Exception:
                pass

    async def handle_reaction_added(self, event: dict, client: AsyncWebClient):
        """Logs the entire thread when a reaction is added to a bot message."""
        try:
            bot_user_id = await self._get_bot_user_id(client)
            if not bot_user_id: return

            reacted_item = event.get("item", {})
            if reacted_item.get("type") != "message": return

            channel = reacted_item.get("channel")
            message_ts = reacted_item.get("ts")
            if not channel or not message_ts: return

            try:
                resp = await client.conversations_history(channel=channel, oldest=message_ts, latest=message_ts, inclusive=True, limit=1)
                if not resp.get("messages"):
                    self.logger.warning(f"Could not find reacted message {message_ts} in {channel}")
                    return
                original_message = resp["messages"][0]
            except SlackApiError as e:
                self.logger.error(f"Cannot fetch reacted message: {e}")
                return

            if original_message.get("user") != bot_user_id: return

            thread_ts = original_message.get("thread_ts") or original_message.get("ts")
            await self._log_thread_for_review(client, channel, thread_ts, event)

        except Exception:
            self.logger.exception("Error handling reaction_added event")

    async def _get_thread_context(self, client: AsyncWebClient, channel: str, thread_ts: str, user_id: str) -> str:
        """Fetch all messages in a thread and format them, fetching user emails concurrently."""
        try:
            history = await client.conversations_replies(channel=channel, ts=thread_ts)
            messages_to_format = []
            user_ids_to_fetch: Set[str] = set()

            for msg in history.get("messages", []):
                msg_user = msg.get("user")
                if 'bot_id' in msg or msg_user == user_id or not msg.get("text", "").strip():
                    continue
                messages_to_format.append(msg)
                if msg_user:
                    user_ids_to_fetch.add(msg_user)
            
            if not messages_to_format:
                return ""

            email_tasks = [self._get_user_email(uid, client) for uid in user_ids_to_fetch]
            emails = await asyncio.gather(*email_tasks)
            email_map = dict(zip(user_ids_to_fetch, emails))

            formatted_messages = []
            for msg in messages_to_format:
                user_email = email_map.get(msg.get("user"), "unknown_user")
                text = remove_bot_mention(msg.get("text", "").strip(), self.bot_user_id)
                formatted_messages.append(f"<@{user_email}>: {text}\n")
            
            return "\n".join(formatted_messages)

        except SlackApiError as e:
            self.logger.error(f"Error fetching thread context: {e}")
            return ""

    async def _stream_agent_response(self, context: str, user_id: str, thread_ts: str, say: AsyncSay, channel_id: str):
        """Stream response from Agent Engine and send final reply."""
        response_chunks = []
        try:
            async for chunk in self.agent_client.stream_query(user_id=user_id, message=context, channel_id=channel_id):
                response_chunks.append(chunk)
        except Exception as e:
            self.logger.error(f"Agent Engine streaming error: {e}")
            await say(text="❌ Sorry, I'm having trouble connecting to the agent service.", thread_ts=thread_ts)
            return

        final_response = "".join(response_chunks) if response_chunks else "🤷 I don't have a response for that."
        await say(text=markdown_to_slack(final_response), thread_ts=thread_ts)

    async def _log_thread_for_review(self, client: AsyncWebClient, channel: str, thread_ts: str, reaction_event: dict):
        """Log complete thread to a file for human review and trigger analysis."""
        try:
            history_response = await client.conversations_replies(channel=channel, ts=thread_ts)
            messages = history_response.get("messages", [])
            if not messages: return

            log_data = {
                "timestamp": datetime.utcnow().isoformat(),
                "bot_name": self.bot_name,
                "channel": channel,
                "thread_ts": thread_ts,
                "reaction_event": reaction_event,
                "thread_messages": messages
            }

            timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"thread_{timestamp_str}_{channel}_{thread_ts.replace('.', '_')}.json"
            log_file_path = self.logs_dir / filename

            with open(log_file_path, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Logged thread to: {log_file_path}")

            reaction_response = analyze_log_vertexai_with_json(log_data, reaction_event.get("user"))
            friendly_message = reaction_response.get("friendly_response_to_user")
            if friendly_message:
                await client.chat_postMessage(channel=channel, text=friendly_message, thread_ts=thread_ts)
                self.logger.info(f"Posted friendly feedback message to thread {thread_ts}")

            cleaned_log = remove_friendly_response_field(reaction_response)
            learning_filename = f"learning_{timestamp_str}_{channel}_{thread_ts.replace('.', '_')}.json"
            upload_json_to_gcs(cleaned_log, learning_filename, self.bot_name)
            self.logger.info(f"Uploaded learning log to GCS for thread {thread_ts}")

        except Exception as e:
            self.logger.error(f"Error logging thread for review: {e}")

    async def replace_mentions_with_emails(self, text: str, client: AsyncWebClient) -> str:
        """Replaces all Slack user mentions with their corresponding user emails concurrently."""
        pattern = r'<@([A-Z0-9]+)>'
        user_ids = set(re.findall(pattern, text))
        if not user_ids:
            return text

        email_tasks = [self._get_user_email(uid, client) for uid in user_ids]
        emails = await asyncio.gather(*email_tasks)
        email_map = dict(zip(user_ids, emails))
        
        def replace_mention(match):
            user_id = match.group(1)
            return email_map.get(user_id, match.group(0))

        return re.sub(pattern, replace_mention, text)

    async def _get_user_email(self, user_id: str, client: AsyncWebClient) -> str:
        """Fetch the email address of a Slack user."""
        try:
            response = await client.users_info(user=user_id)
            if response["ok"]:
                profile = response.get("user", {}).get("profile", {})
                if profile.get("email"):
                    return profile["email"]
                if response.get("user", {}).get("is_bot"):
                    return response.get("user", {}).get("real_name", user_id)
        except SlackApiError as e:
            self.logger.warning(f"Failed to get email for user {user_id}: {e}")
        return user_id
