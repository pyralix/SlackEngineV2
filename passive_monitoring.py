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


class PassiveMessageHandler:
    def __init__(self, client: AsyncWebClient, agent_engine_client: AgentEngineClient, config):
        self.client = client
        self.agent_engine_client = agent_engine_client
        self.config = config
        self.watched_threads = {}

    async def handle_message(self, event: dict):
        """
        This method is called for every message in the channels the bot is in.
        It checks if the message is a candidate for passive monitoring and, if so,
        adds it to the list of watched_threads.
        """
        # Ignore messages from bots or with mentions
        if event.get("bot_id") or "<@" in event.get("text", ""):
            return

        # If a message is in a thread, and that thread is being watched, remove it from watched_threads
        if event.get("thread_ts"):
            thread_key = f"{event.get('channel')}-{event.get('thread_ts')}"
            if thread_key in self.watched_threads:
                # Check if the message is a reply from another user
                if event.get("user") != self.watched_threads[thread_key]["user_id"]:
                    del self.watched_threads[thread_key]
                    logging.info(f"Thread {thread_key} has a reply, removing from watch list.")
            return

        channel_id = event.get("channel")
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
        logging.info(f"Watching new thread: {thread_key}")

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
                        logging.info(f"Thread {thread_key} is an unanswered technical question.")
                        response = await self._generate_response(thread_data["text"], thread_data["user_id"])
                        await self.client.chat_postMessage(
                            channel=thread_data["channel_id"],
                            thread_ts=thread_data["thread_ts"],
                            text=f"{response}\n\n(To continue this conversation, please mention me with `@Ask EDE`)"
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
        prompt = f"Is the following a technical question that you would like to try answering? Respond with only 'yes' or 'no'.\n\n{message}"
        response = ""
        async for chunk in self.agent_engine_client.stream_query(None, "passive_monitoring", prompt):
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
