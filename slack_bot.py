"""
Simplified Slack bot runner for single process execution.
"""

import asyncio
import logging
from typing import Set, Optional
from aiohttp import web

from slack_bolt.async_app import AsyncApp
from config_loader import Config
from agent_engine_client import AgentEngineClient, AgentEngineConfig
from slack_message_handler import EnhancedSlackMessageHandler
from session_manager import SessionManager
from passive_monitoring import PassiveMessageHandler
from thread_link_storage import ThreadLinkStorage


class SlackBot:
    """
    Simplified Slack bot that runs in the main process.
    """
    
    def __init__(self, config: Config, port: int):
        """
        Initialize the Slack bot.
        """
        self.config = config
        self.port = port
        self.logger = logging.getLogger(f"{__name__}.{config.slack_bot.name}")
        self.background_tasks: Set[asyncio.Task] = set()
        self.aiohttp_runner: Optional[web.AppRunner] = None

        # Initialize shared components
        self.thread_linker = ThreadLinkStorage()

        # Initialize Slack Bolt app
        self.app = AsyncApp(
            token=config.slack_bot.bot_token,
            signing_secret=config.slack_bot.signing_secret
        )
        
        # Initialize session manager
        self.session_manager = SessionManager(
            ttl_minutes=config.global_settings.session_timeout_minutes
        )
        
        # Initialize Agent Engine client
        agent_config = AgentEngineConfig(
            api_key=config.agent_engine.api_key,
            endpoint=config.agent_engine.endpoint,
            project=config.agent_engine.project,
            location=config.agent_engine.location,
            reasoning_engine_id=config.agent_engine.reasoning_engine_id,
            session_storage_path=config.agent_engine.session_storage_path
        )
        self.agent_client = AgentEngineClient(agent_config)
        
        # Initialize message handlers, passing the shared thread_linker
        self.message_handler = EnhancedSlackMessageHandler(
            self.session_manager,
            self.agent_client,
            bot_name=config.slack_bot.name,
            thread_linker=self.thread_linker
        )

        self.passive_message_handler = PassiveMessageHandler(
            self.app.client,
            self.agent_client,
            self.config.passive_monitoring,
            thread_linker=self.thread_linker
        )
        
        # Register event handlers
        self._register_handlers()
        
        self.logger.info(f"Initialized bot '{config.slack_bot.name}' for port {port}")
    
    def _register_handlers(self):
        """Register Slack event handlers."""
        
        @self.app.event("message")
        async def handle_message(event, say, client):
            """Handle incoming messages."""
            # The EnhancedSlackMessageHandler now handles both relay commands and regular mentions.
            await self.message_handler.handle_message(event, say, client)
            # The PassiveMessageHandler handles non-mentions in monitored channels.
            await self.passive_message_handler.handle_message(event)

        @self.app.event("app_mention")
        async def handle_app_mention(event, say, client):
            """Handle app mentions (same as regular messages)."""
            await self.message_handler.handle_message(event, say, client)
        
        @self.app.event("reaction_added")
        async def handle_reaction_added(event, client):
            """Handle reactions added to messages."""
            await self.message_handler.handle_reaction_added(event, client)
        
        self.logger.info("Registered Slack event handlers")
    
    def _create_background_task(self, coro):
        """Create and track a background task."""
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def start_async(self):
        """
        Start the Slack bot and background tasks.
        """
        self.logger.info(f"Starting Slack bot server on port {self.port}")
        
        self._create_background_task(self.cleanup_sessions())
        self._create_background_task(self.review_threads_periodically())

        server = self.app.server(port=self.port, path="/slack/events")
        self.aiohttp_runner = web.AppRunner(server.web_app)
        await self.aiohttp_runner.setup()
        
        site = web.TCPSite(self.aiohttp_runner, host="0.0.0.0", port=self.port)
        await site.start()
        
        self.logger.info("Bolt app is running!")

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.logger.info("Main server task cancelled, initiating shutdown.")

    async def stop(self):
        """Stop the Slack bot and its background tasks gracefully."""
        self.logger.info("Stopping Slack bot and background tasks...")
        
        tasks = list(self.background_tasks)
        if tasks:
            self.logger.info(f"Cancelling {len(tasks)} background tasks...")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.logger.info("All background tasks have been cancelled.")

        if self.aiohttp_runner:
            await self.aiohttp_runner.cleanup()
            self.logger.info("AIOHTTP server runner cleaned up.")

    async def cleanup_sessions(self):
        """Periodic cleanup of old sessions."""
        while True:
            try:
                await asyncio.sleep(300)
                cutoff = self.config.global_settings.session_timeout_minutes * 60
                self.session_manager.purge_old(cutoff)
                self.logger.debug("Cleaned up old sessions")
            except asyncio.CancelledError:
                self.logger.info("Session cleanup task cancelled.")
                break
            except Exception as e:
                self.logger.error(f"Error during session cleanup: {e}")

    async def review_threads_periodically(self):
        """Periodically review watched threads."""
        while True:
            try:
                await asyncio.sleep(300)
                await self.passive_message_handler.review_watched_threads()
            except asyncio.CancelledError:
                self.logger.info("Thread review task cancelled.")
                break
            except Exception as e:
                self.logger.error(f"Error during passive thread review: {e}")
