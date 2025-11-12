"""
Simplified entry point for single Slack bot with Agent Engine.

This runs a single Slack bot instance connected to an Agent Engine,
taking the port as a command line argument.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from config_loader import load_config
from slack_bot import SlackBot


def setup_logging(log_level: str = "INFO"):
    """Setup logging configuration."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('slack_bot.log')
        ]
    )


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Run a single Slack bot with Agent Engine'
    )
    parser.add_argument(
        '--port', '-p',
        type=int,
        required=True,
        help='Port number for the Slack HTTP server'
    )
    parser.add_argument(
        '--config', '-c',
        type=str,
        default='config.json',
        help='Path to configuration file (default: config.json)'
    )
    return parser.parse_args()


async def main_async():
    """Main asynchronous entry point."""
    args = parse_arguments()

    # Load configuration
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        sys.exit(1)

    # Setup logging
    setup_logging(config.global_settings.log_level)
    logger = logging.getLogger(__name__)

    # Validate configuration file exists
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Configuration file not found: {args.config}")
        sys.exit(1)

    # Create the Slack bot
    bot = SlackBot(config, args.port)
    
    main_task = None
    try:
        # Create a task for the bot's main run function
        main_task = asyncio.create_task(bot.start_async())
        logger.info(f"Starting Slack bot '{config.slack_bot.name}' on port {args.port}")
        logger.info(f"Using configuration: {args.config}")
        await main_task
        
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown initiated...")
        
    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)
        sys.exit(1)
        
    finally:
        if main_task and not main_task.done():
            main_task.cancel()
        
        # Ensure graceful shutdown of background tasks
        await bot.stop()
        logger.info("Slack bot stopped.")


if __name__ == '__main__':
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        # This allows Ctrl+C to exit without a traceback
        pass
