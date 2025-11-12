"""Simplified configuration loader for single bot-agent pair."""

from pydantic import BaseModel
import json
from pathlib import Path


class SlackBotConfig(BaseModel):
    """Configuration for a single Slack bot."""
    name: str
    bot_token: str
    signing_secret: str


class AgentEngineConfig(BaseModel):
    """Configuration for a single Agent Engine."""
    api_key: str  # Bearer token for Agent Engine
    endpoint: str
    project: str
    location: str
    reasoning_engine_id: str
    session_storage_path: str = "session.json"


class GlobalSettings(BaseModel):
    """Global application settings."""
    log_level: str = "INFO"
    session_timeout_minutes: int = 30


class PassiveMonitoringConfig(BaseModel):
    """Configuration for passive monitoring."""
    no_response_timeout_minutes: int = 480  # Default to 8 hours (480 minutes)


class Config(BaseModel):
    """Simplified configuration for single bot-agent pair."""
    global_settings: GlobalSettings
    slack_bot: SlackBotConfig
    agent_engine: AgentEngineConfig
    passive_monitoring: PassiveMonitoringConfig


def load_config(path: str) -> Config:
    """
    Load configuration from JSON file.
    
    Args:
        path: Path to the configuration JSON file
        
    Returns:
        Config: Parsed configuration object
        
    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config format is invalid
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return Config.model_validate(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config file: {e}")
    except Exception as e:
        raise ValueError(f"Invalid configuration format: {e}")