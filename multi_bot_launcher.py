#!/usr/bin/env python3
"""
Simple multi-bot launcher script.

This replaces the orchestrator by launching multiple bot processes.
Each bot runs in its own process for better isolation and Linux compatibility.
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Dict, Any


class MultiBotLauncher:
    """
    Simple launcher for multiple Slack bot processes.
    
    This replaces the complex orchestrator with a simple process launcher
    that starts each bot in its own process for better isolation.
    """
    
    def __init__(self):
        self.processes: List[subprocess.Popen] = []
        self.logger = logging.getLogger(__name__)
    
    def launch_bots(self, configs: List[Dict[str, Any]]):
        """
        Launch multiple bot processes.
        
        Args:
            configs: List of configuration dictionaries with 'config_file' and 'port'
        """
        for bot_config in configs:
            config_file = bot_config['config_file']
            port = bot_config['port']
            
            if not Path(config_file).exists():
                self.logger.error(f"Config file not found: {config_file}")
                continue
            
            try:
                # Launch bot process
                cmd = [
                    sys.executable,
                    'main.py',
                    '--port', str(port),
                    '--config', config_file
                ]
                
                self.logger.info(f"Starting bot with config {config_file} on port {port}")
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )
                
                self.processes.append(process)
                self.logger.info(f"Started bot process PID {process.pid}")
                
                # Give process time to start
                time.sleep(2)
                
            except Exception as e:
                self.logger.error(f"Failed to start bot with config {config_file}: {e}")
    
    def monitor_processes(self):
        """Monitor bot processes and restart if they fail."""
        while True:
            try:
                for i, process in enumerate(self.processes):
                    if process.poll() is not None:
                        # Process has terminated
                        self.logger.warning(f"Bot process PID {process.pid} has terminated with code {process.returncode}")
                        
                        # Read any remaining output
                        if process.stdout:
                            output = process.stdout.read()
                            if output:
                                self.logger.info(f"Process output: {output}")
                
                time.sleep(10)  # Check every 10 seconds
                
            except KeyboardInterrupt:
                self.logger.info("Received shutdown signal")
                self.shutdown_all()
                break
            except Exception as e:
                self.logger.error(f"Error monitoring processes: {e}")
                time.sleep(10)
    
    def shutdown_all(self):
        """Shutdown all bot processes gracefully."""
        self.logger.info("Shutting down all bot processes...")
        
        for process in self.processes:
            if process.poll() is None:  # Process is still running
                try:
                    self.logger.info(f"Terminating process PID {process.pid}")
                    process.terminate()
                    
                    # Wait for graceful shutdown
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.logger.warning(f"Force killing process PID {process.pid}")
                        process.kill()
                        process.wait()
                    
                except Exception as e:
                    self.logger.error(f"Error shutting down process PID {process.pid}: {e}")
        
        self.logger.info("All processes shut down")


def load_multi_bot_config(config_file: str) -> List[Dict[str, Any]]:
    """
    Load multi-bot configuration file.
    
    Expected format:
    {
      "bots": [
        {"config_file": "bot1_config.json", "port": 3000},
        {"config_file": "bot2_config.json", "port": 3001}
      ]
    }
    
    Args:
        config_file: Path to multi-bot configuration file
        
    Returns:
        List of bot configurations
    """
    try:
        with open(config_file, 'r') as f:
            data = json.load(f)
        return data.get('bots', [])
    except Exception as e:
        logging.error(f"Error loading multi-bot config: {e}")
        return []


def setup_logging():
    """Setup logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('multi_bot_launcher.log')
        ]
    )


def main():
    """Main entry point for multi-bot launcher."""
    parser = argparse.ArgumentParser(
        description='Launch multiple Slack bot processes'
    )
    parser.add_argument(
        '--config', '-c',
        type=str,
        default='multi_bot_config.json',
        help='Multi-bot configuration file (default: multi_bot_config.json)'
    )
    parser.add_argument(
        '--single', '-s',
        nargs=2,
        metavar=('CONFIG_FILE', 'PORT'),
        help='Launch single bot with config file and port'
    )
    
    args = parser.parse_args()
    
    setup_logging()
    logger = logging.getLogger(__name__)
    
    launcher = MultiBotLauncher()
    
    try:
        if args.single:
            # Launch single bot
            config_file, port = args.single
            configs = [{'config_file': config_file, 'port': int(port)}]
        else:
            # Launch multiple bots from config
            configs = load_multi_bot_config(args.config)
            if not configs:
                logger.error(f"No bot configurations found in {args.config}")
                sys.exit(1)
        
        logger.info(f"Launching {len(configs)} bot processes")
        launcher.launch_bots(configs)
        
        if launcher.processes:
            logger.info("Starting process monitoring...")
            launcher.monitor_processes()
        else:
            logger.error("No bot processes were started successfully")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error(f"Launcher error: {e}")
        sys.exit(1)
    finally:
        launcher.shutdown_all()


if __name__ == '__main__':
    main()