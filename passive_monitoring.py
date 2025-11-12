"""
This module contains the PassiveMessageHandler class, which is responsible for
monitoring channels for messages that do not mention the bot, and providing
autonomous responses to unanswered technical questions.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from slack_sdk.web.async_client import AsyncWebClient
from agent_engine_client import AgentEngineClient
from config_loader import PassiveMonitoringConfig


class PassiveMessageHandler:
    def __init__(self, client: AsyncWebClient, agent_engine_client: AgentEngineClient, config: PassiveMonitoringConfig):
        self.client = client
        self.agent_engine_client = agent_engine_client
        self.config = config
        self.watched_threads = {}
        # Create a quick lookup for monitored channels
        self.monitored_channels = {
            mapping.monitored_channel_id: mapping.notification_channel_id
            for mapping in self.config.channel_mappings
        }

    async def handle_message(self, event: dict):
        """
        This method is called for every message in the channels the bot is in.
        It checks if the message is a candidate for passive monitoring and, if so,
        adds it to the list of watched_threads.
        """
        channel_id = event.get("channel")

        # 1. Only monitor channels defined in the config
        if channel_id not in self.monitored_channels:
            return

        # 2. Ignore messages from bots or with mentions
        if event.get("bot_id") or "<@" in event.get("text", ""):
            return

        # 3. If a message is a reply in a watched thread, remove it from the watch list
        if event.get("thread_ts"):
            thread_key = f"{channel_id}-{event.get('thread_ts')}"
            if thread_key in self.watched_threads:
                # Check if the reply is from a different user
                if event.get("user") != self.watched_threads[thread_key]["user_id"]:
                    del self.watched_threads[thread_key]
                    logging.info(f"Thread {thread_key} received a reply, removing from watch list.")
            return

        # 4. If it's a new message in a monitored channel, add it to the watch list
        message_ts = event.get("ts")
        user_id = event.get("user")
        text = event.get("text")

        if not all([channel_id, message_ts, user_id, text]):
            return

        thread_key = f"{channel_id}-{message_ts}"
        self.watched_threads[thread_key] = {
            "channel_id": channel_id,
            "thread_ts": message_ts,
            "user_id": user_id,
            "text": text,
            "timestamp": datetime.now(),
        }
        logging.info(f"Watching new thread in channel {channel_id}: {thread_key}")

    async def review_watched_threads(self):
        """
        This method is called periodically to review the watched_threads.
        It checks if the timeout has expired for any of the threads and, if so,
        determines if a response is warranted.
        """
        now = datetime.now()
        timeout = timedelta(minutes=self.config.no_response_timeout_minutes)
        threads_to_remove = []

        for thread_key, thread_data in self.watched_threads.items():
            if now - thread_data["timestamp"] > timeout:
                threads_to_remove.append(thread_key)
                try:
                    # Check for replies in the thread
                    replies = await self.client.conversations_replies(
                        channel=thread_data["channel_id"],
                        ts=thread_data["thread_ts"],
                        limit=1
                    )
                    if len(replies.get("messages", [])) > 1:
                        logging.info(f"Thread {thread_key} has replies, removing from watch list.")
                        continue

                    # If no replies, check if it's a technical question
                    if await self._is_technical_question(thread_data["text"]):
                        logging.info(f"Thread {thread_key} is an unanswered technical question. Responding.")
                        
                        # Generate the helpful response for the user
                        response = await self._generate_response(thread_data["text"], thread_data["user_id"])
                        await self.client.chat_postMessage(
                            channel=thread_data["channel_id"],
                            thread_ts=thread_data["thread_ts"],
                            text=f"{response}\n\n(To continue this conversation, please mention me with `@Ask EDE`)"
                        )

                        # Generate and send the notification message
                        notification_channel_id = self.monitored_channels.get(thread_data["channel_id"])
                        if notification_channel_id:
                            notification_text = await self._generate_notification(thread_data["text"], response)
                            await self.client.chat_postMessage(
                                channel=notification_channel_id,
                                text=notification_text
                            )

                except Exception as e:
                    logging.error(f"Error processing watched thread {thread_key}: {e}")

        for thread_key in threads_to_remove:
            if thread_key in self.watched_threads:
                del self.watched_threads[thread_key]

    async def _is_technical_question(self, message: str) -> bool:
        """
        This method uses the agent engine to determine if a message is a
        technical question that the bot can answer.
        """
        prompt = f"Is the following a technical question that can be answered? Respond with only 'yes' or 'no'.\n\n{message}"
        response = ""
        async for chunk in self.agent_engine_client.stream_query(None, "passive_monitoring_classifier", prompt):
            response += chunk
        return "yes" in response.lower()

    async def _generate_response(self, message: str, user_id: str) -> str:
        """
        This method uses the agent engine to generate a response to a
        technical question.
        """
        prompt = f"The following is a question from a user. Please provide a helpful response.\n\n{message}"
        response = ""
        async for chunk in self.agent_engine_client.stream_query(None, user_id, prompt):
            response += chunk
        return response

    async def _generate_notification(self, user_question: str, my_response: str) -> str:
        """
        Generates a first-person notification about the autonomous action taken.
        """
        prompt = (
            "You are an AI assistant. You just responded to a user's question automatically because no one else did. "
            "Now, write a brief, first-person notification for an internal channel to explain what happened. "
            "Be friendly and concise.\n\n"
            f"This was the user's question: \"{user_question}\"\n\n"
            f"This was your helpful response: \"{my_response}\""
        )
        notification = ""
        async for chunk in self.agent_engine_client.stream_query(None, "passive_monitoring_notifier", prompt):
            notification += chunk
        return notification
