"""AgentEngineClient using Google's streaming Reasoning Engine over SSE."""

import httpx
from typing import AsyncIterator
from dataclasses import dataclass
from auth_token_generator import get_token
import json
import logging


@dataclass(frozen=True)
class AgentEngineConfig:
    api_key: str  # Used as Google API Bearer token
    endpoint: str
    project: str
    location: str
    reasoning_engine_id: str


class AgentEngineClient:
    def __init__(self, config: AgentEngineConfig):
        self.config = config
        self.token = config.api_key  # Treat api_key as bearer token

    def _build_url(self) -> str:
        return (
            f"{self.config.endpoint}/projects/{self.config.project}"
            f"/locations/{self.config.location}/reasoningEngines/"
            f"{self.config.reasoning_engine_id}:streamQuery?alt=sse"
        )

    async def stream_query(self, user_id: str, message: str) -> AsyncIterator[str]:
        """
        Stream reply from AgentEngine via SSE.

        :param user_id: Slack user ID
        :param message: Message history or text prompt
        :yield: Partial streamed responses
        """
        token = get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        payload = {
            "class_method": "stream_query",
            "input": {
                "message": message,
                "user_id": user_id
            }
        }

        url = self._build_url()

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    # Read the full error body for context before returning
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
