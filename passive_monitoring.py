"""
This module contains the PassiveMessageHandler class, which is responsible for
monitoring channels for messages that do not mention the bot, and providing
autonomous responses to unanswered technical questions.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from slack_sdk.web.async_client import AsyncWebClient
from agent_engine_client import AgentEngineClient
from config_loader import PassiveMonitoringConfig
from thread_link_storage import ThreadLinkStorage
from metrics_tracker import MetricsCSVTracker
from slack_message_handler import markdown_to_slack


class PassiveMessageHandler:
    def __init__(
        self,
        client: AsyncWebClient,
        agent_engine_client: AgentEngineClient,
        config: PassiveMonitoringConfig,
        thread_linker: ThreadLinkStorage,
        metrics_tracker: MetricsCSVTracker,
    ):
        self.client = client
        self.agent_engine_client = agent_engine_client
        self.config = config
        self.thread_linker = thread_linker
        self.metrics_tracker = metrics_tracker
        self.watched_threads = {}
        self.monitored_channels = {
            mapping.monitored_channel_id: mapping.notification_channel_id
            for mapping in self.config.channel_mappings
        }

    async def handle_message(self, event: dict):
        """
        Checks if a message is a candidate for passive monitoring and adds it to the watch list.
        """
        channel_id = event.get("channel")

        if channel_id not in self.monitored_channels:
            return
        if event.get("bot_id") or "<@" in event.get("text", ""):
            return

        if event.get("thread_ts"):
            thread_key = f"{channel_id}-{event.get('thread_ts')}"
            if thread_key in self.watched_threads:
                if event.get("user") != self.watched_threads[thread_key]["user_id"]:
                    del self.watched_threads[thread_key]
                    logging.info(f"Thread {thread_key} received a reply, removing from watch list.")
                    self.metrics_tracker.log_event(
                        "thread_resolved_by_human",
                        channel_id=channel_id,
                        thread_ts=event.get("thread_ts"),
                    )
            return

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
        self.metrics_tracker.log_event("thread_watched", channel_id=channel_id, thread_ts=message_ts)

    async def review_watched_threads(self):
        """
        Periodically reviews watched threads and processes expired ones in parallel.
        """
        now = datetime.now()
        timeout = timedelta(minutes=self.config.no_response_timeout_minutes)
        
        expired_threads = []
        threads_to_keep = {}

        for thread_key, thread_data in self.watched_threads.items():
            if now - thread_data["timestamp"] > timeout:
                expired_threads.append(thread_data)
            else:
                threads_to_keep[thread_key] = thread_data
        
        self.watched_threads = threads_to_keep

        if not expired_threads:
            return

        logging.info(f"Processing {len(expired_threads)} expired threads in parallel.")
        
        tasks = [self._process_single_thread(thread_data) for thread_data in expired_threads]
        await asyncio.gather(*tasks)

    async def _process_single_thread(self, thread_data: Dict[str, Any]):
        """
        Processes a single expired thread to determine if a response is warranted.
        """
        thread_key = f"{thread_data['channel_id']}-{thread_data['thread_ts']}"
        try:
            replies = await self.client.conversations_replies(
                channel=thread_data["channel_id"], ts=thread_data["thread_ts"], limit=1
            )
            if len(replies.get("messages", [])) > 1:
                logging.info(f"Thread {thread_key} has replies, skipping autonomous response.")
                return

            if await self._is_technical_question(thread_data["text"]):
                logging.info(f"Thread {thread_key} is an unanswered technical question. Responding.")
                
                response = await self._generate_response(thread_data["text"], thread_data["user_id"])
                formatted_response = markdown_to_slack(response)
                
                await self.client.chat_postMessage(
                    channel=thread_data["channel_id"],
                    thread_ts=thread_data["thread_ts"],
                    text=f"{formatted_response}\n\n(To continue this conversation, please mention me with `@Ask EDE`)"
                )
                
                self.metrics_tracker.log_event(
                    "autonomous_response",
                    channel_id=thread_data["channel_id"],
                    thread_ts=thread_data["thread_ts"],
                    time_saved_minutes=self.metrics_tracker.config.time_saved_per_autonomous_response_minutes,
                )

                notification_channel_id = self.monitored_channels.get(thread_data["channel_id"])
                if notification_channel_id:
                    notification_text = await self._generate_notification(thread_data["text"], response)
                    
                    permalink_response = await self.client.chat_getPermalink(
                        channel=thread_data["channel_id"], message_ts=thread_data["thread_ts"]
                    )
                    permalink = permalink_response.get("permalink")
                    
                    if permalink:
                        notification_text += f"\n\nYou can find the thread here: {permalink}"

                    notification_post_response = await self.client.chat_postMessage(
                        channel=notification_channel_id, text=notification_text
                    )
                    
                    notification_ts = notification_post_response.get("ts")
                    if notification_ts:
                        self.thread_linker.create_link(
                            notification_ts=notification_ts,
                            original_channel_id=thread_data["channel_id"],
                            original_thread_ts=thread_data["thread_ts"],
                        )
        except Exception as e:
            logging.error(f"Error processing watched thread {thread_key}: {e}", exc_info=True)

    async def _is_technical_question(self, message: str) -> bool:
        prompt = f"Is the following a technical question that can be answered? Respond with only 'yes' or 'no'.\n\n{message}"
        response = ""
        async for chunk in self.agent_engine_client.stream_query(None, "passive_monitoring_classifier", prompt):
            response += chunk
        return "yes" in response.lower()

    async def _generate_response(self, message: str, user_id: str) -> str:
        prompt = (
            "You are an AI assistant. A user asked a question that has gone unanswered. "
            "Please provide a helpful, first-person response to their question. "
            "Also, let them know that you have notified your support team and that someone will get back to them if more help is needed."
            f"\n\nHere is the user's question: \"{message}\""
        )
        response = ""
        async for chunk in self.agent_engine_client.stream_query(None, user_id, prompt):
            response += chunk
        return response

    async def _generate_notification(self, user_question: str, my_response: str) -> str:
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
