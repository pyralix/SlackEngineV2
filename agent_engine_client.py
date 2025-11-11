"""AgentEngineClient using Google's streaming Reasoning Engine over SSE with session management."""

import httpx
from typing import AsyncIterator
from dataclasses import dataclass
from auth_token_generator import get_token
import json
import logging
import os
import threading
from typing import Optional, Tuple
from config_loader import Config


@dataclass(frozen=True)
class AgentEngineConfig:
    api_key: str  # Used as Google API Bearer token
    endpoint: str
    project: str
    location: str
    reasoning_engine_id: str
    session_storage_path: str = None  # Path to sessions.json, defaults to 'sessions.json'


class SessionStorage:
    """Thread-safe persistent storage for sessions in JSON file."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Create the file if it doesn't exist."""
        if not os.path.exists(self.path):
            with open(self.path, 'w') as f:
                json.dump([], f)
            logging.info(f"Created sessions file at {self.path}")

    def _load_sessions(self) -> list:
        """Load sessions from file."""
        with self.lock:
            try:
                with open(self.path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return []

    def _save_sessions(self, sessions: list):
        """Save sessions to file."""
        with self.lock:
            with open(self.path, 'w') as f:
                json.dump(sessions, f, indent=2)

    def get_session(self, channel_id: str, user_id: str) -> Optional[dict]:
        """Retrieve session by (channel_id, user_id) tuple. Falls back to user_id only if channel_id is None."""
        sessions = self._load_sessions()
        key = (channel_id, user_id) if channel_id else (None, user_id)
        for session in sessions:
            session_key = (session.get('channel_id'), session['user_id'])
            if session_key == key:
                return session
        return None

    def create_session(self, channel_id: str, user_id: str, session_id: str):
        """Create and store new session."""
        sessions = self._load_sessions()
        new_session = {
            'channel_id': channel_id,
            'user_id': user_id,
            'session_id': session_id
        }
        sessions.append(new_session)
        self._save_sessions(sessions)
        logging.info(f"Created and stored session {session_id} for {channel_id}:{user_id}")

    def update_session(self, channel_id: str, user_id: str, session_id: str):
        """Update existing session ID (e.g., if recreated)."""
        sessions = self._load_sessions()
        key = (channel_id, user_id)
        for session in sessions:
            if (session.get('channel_id'), session['user_id']) == key:
                session['session_id'] = session_id
                self._save_sessions(sessions)
                logging.info(f"Updated session {session_id} for {channel_id}:{user_id}")
                return
        # If not found, create new
        self.create_session(channel_id, user_id, session_id)


class AgentEngineClient:
    def __init__(self, config: AgentEngineConfig):
        self.config = config
        self.token = config.api_key  # Treat api_key as bearer token
        storage_path = config.session_storage_path
        self.storage = SessionStorage(storage_path)
        self._build_session_url = (
            f"{self.config.endpoint}/projects/{self.config.project}"
            f"/locations/{self.config.location}/reasoningEngines/"
            f"{self.config.reasoning_engine_id}:query"
        )

    def _build_stream_url(self) -> str:
        return (
            f"{self.config.endpoint}/projects/{self.config.project}"
            f"/locations/{self.config.location}/reasoningEngines/"
            f"{self.config.reasoning_engine_id}:streamQuery?alt=sse"
        )

    async def _create_new_session(self, user_id: str) -> str:
        """Create a new session via API and return session_id."""
        token = get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "class_method": "async_create_session",
            "input": {"user_id": user_id}
        }

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", self._build_session_url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    logging.error(
                        f"Session creation failed: {response.status_code}, "
                        f"body: {error_text.decode(errors='replace')}"
                    )
                    raise ValueError(f"Failed to create session: HTTP {response.status_code}")

                # Read the full response body
                response_text = await response.aread()
                try:
                    obj = json.loads(response_text.decode('utf-8'))
                    session_id = obj.get("output", {}).get("id")
                    if not session_id:
                        raise ValueError("No session_id in response")
                    return session_id
                except (json.JSONDecodeError, KeyError) as e:
                    logging.error(f"Failed to parse session response: {e}, body: {response_text.decode(errors='replace')}")
                    raise ValueError(f"Invalid session response: {e}")

    async def get_or_create_session(self, channel_id: Optional[str], user_id: str) -> str:
        """
        CRUD: Get existing session_id or create/store new one based on (channel_id, user_id).

        :param channel_id: Slack channel ID (optional, but recommended for multi-channel)
        :param user_id: Slack user ID
        :return: session_id (str)
        """
        existing = self.storage.get_session(channel_id or "", user_id)
        if existing:
            logging.info(f"Retrieved existing session {existing['session_id']} for {channel_id}:{user_id}")
            return existing['session_id']

        # Create new
        session_id = await self._create_new_session(user_id)
        self.storage.create_session(channel_id or "", user_id, session_id)
        return session_id

    async def stream_query(self, channel_id: Optional[str], user_id: str, message: str) -> AsyncIterator[str]:
        """
        Stream reply from AgentEngine via SSE using dynamic session_id.

        :param channel_id: Slack channel ID (for session key)
        :param user_id: Slack user ID
        :param message: Message history or text prompt
        :yield: Partial streamed responses
        """
        session_id = await self.get_or_create_session(channel_id, user_id)

        token = get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        payload = {
            "class_method": "async_stream_query",
            "input": {
                "message": message,
                "session_id": session_id,
                "user_id": user_id
            }
        }

        url = self._build_stream_url()

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    logging.error(
                        f"Received non-200 status: {response.status_code}, "
                        f"response body: {error_text.decode(errors='replace')}"
                    )
                    return  # Or raise an exception, if you want the caller to handle it

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        parts = obj.get("content", {}).get("parts", [])
                        if not parts:
                            continue

                        first_part = parts[0]

                        # Fallback to text if no function_response
                        text = first_part.get("text")
                        if text:
                            yield text

                    except (ValueError, KeyError, TypeError) as e:
                        # Optionally log line or exception here
                        logging.debug(f"Error parsing stream line: {e}, line: {line}")
                        continue
