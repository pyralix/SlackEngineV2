"""
Simplified Slack bot runner for single process execution.

This replaces the multi-threaded orchestrator approach with a simple
single-process Slack bot that connects to one Agent Engine.
"""

import asyncio
import logging
from slack_bolt.async_app import AsyncApp
from config_loader import Config
from agent_engine_client import AgentEngineClient, AgentEngineConfig
from slack_message_handler import EnhancedSlackMessageHandler
from session_manager import SessionManager


class SlackBot:
    """
    Simplified Slack bot that runs in the main process.
    
    This class handles a single Slack bot instance connected to one Agent Engine,
    running the HTTP server in the main process instead of a thread for better
    Linux compatibility and signal handling.
    """
    
    def __init__(self, config: Config, port: int):
        """
        Initialize the Slack bot.
        
        Args:
            config: Configuration object with bot and agent settings
            port: Port number for the HTTP server
        """
        self.config = config
        self.port = port
        self.logger = logging.getLogger(f"{__name__}.{config.slack_bot.name}")
        
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
        
        # Initialize message handler with reaction logging support
        self.message_handler = EnhancedSlackMessageHandler(
            self.session_manager,
            self.agent_client,
            bot_name=config.slack_bot.name
        )
        
        # Register event handlers
        self._register_handlers()
        
        self.logger.info(f"Initialized bot '{config.slack_bot.name}' for port {port}")
    
    def _register_handlers(self):
        """Register Slack event handlers."""
        
        @self.app.event("message")
        async def handle_message(event, say, client):
            """Handle incoming messages."""
            await self.message_handler.handle_message(event, say, client)
        
        @self.app.event("app_mention")
        async def handle_app_mention(event, say, client):
            """Handle app mentions (same as regular messages)."""
            await self.message_handler.handle_message(event, say, client)
        
        @self.app.event("reaction_added")
        async def handle_reaction_added(event, client):
            """Handle reactions added to messages."""
            await self.message_handler.handle_reaction_added(event, client)
        
        self.logger.info("Registered Slack event handlers")
    
    def run(self):
        """
        Start the Slack bot HTTP server.
        
        This runs in the main process and blocks until stopped.
        Uses Slack Bolt's built-in HTTP server for better signal handling.
        """
        try:
            self.logger.info(f"Starting Slack bot server on port {self.port}")
            
            # Start the bot with HTTP mode - this blocks
            self.app.start(
                port=self.port,
                path="/slack/events"
            )
            
        except Exception as e:
            self.logger.error(f"Error starting bot server: {e}")
            raise
    
    def stop(self):
        """Stop the Slack bot gracefully."""
        self.logger.info("Stopping Slack bot...")
        # Slack Bolt handles cleanup automatically when the process ends
    
    async def cleanup_sessions(self):
        """Periodic cleanup of old sessions."""
        while True:
            try:
                await asyncio.sleep(300)  # Cleanup every 5 minutes
                cutoff = self.config.global_settings.session_timeout_minutes * 60
                self.session_manager.purge_old(cutoff)
                self.logger.debug("Cleaned up old sessions")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error during session cleanup: {e}")