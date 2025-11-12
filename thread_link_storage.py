"""
Persistent, thread-safe storage for linking notification messages to original threads.
"""

import json
import logging
import os
import threading
from typing import Optional, Dict


class ThreadLinkStorage:
    """
    Manages a persistent JSON file to store links between the timestamp of a
    notification message and the original user's thread information.
    """

    def __init__(self, path: str = "thread_links.json"):
        """
        Initializes the storage.

        Args:
            path: The path to the JSON file for storage. Defaults to 'thread_links.json'.
        """
        self.path = path
        self.lock = threading.Lock()
        self._ensure_file_exists()
        logging.info(f"Initialized ThreadLinkStorage with file at {self.path}")

    def _ensure_file_exists(self):
        """Create the file if it doesn't exist with an empty JSON object."""
        if not os.path.exists(self.path):
            with open(self.path, 'w') as f:
                json.dump({}, f)
            logging.info(f"Created thread links file at {self.path}")

    def _load_links(self) -> Dict:
        """Load the links from the JSON file."""
        with self.lock:
            try:
                with open(self.path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return {}

    def _save_links(self, links: Dict):
        """Save the links to the JSON file."""
        with self.lock:
            with open(self.path, 'w') as f:
                json.dump(links, f, indent=2)

    def create_link(self, notification_ts: str, original_channel_id: str, original_thread_ts: str):
        """
        Create and store a link from a notification message to an original thread.

        Args:
            notification_ts: The timestamp ('ts') of the message posted in the notification channel.
            original_channel_id: The channel ID of the original user's message.
            original_thread_ts: The thread timestamp ('thread_ts') of the original user's message.
        """
        links = self._load_links()
        links[notification_ts] = {
            "original_channel_id": original_channel_id,
            "original_thread_ts": original_thread_ts,
        }
        self._save_links(links)
        logging.info(f"Created thread link: {notification_ts} -> {original_channel_id}/{original_thread_ts}")

    def get_link(self, notification_ts: str) -> Optional[Dict]:
        """
        Retrieve the original thread information using the notification message's timestamp.

        Args:
            notification_ts: The timestamp ('ts' or 'thread_ts') of the message in the notification channel.

        Returns:
            A dictionary containing 'original_channel_id' and 'original_thread_ts', or None if not found.
        """
        links = self._load_links()
        link_info = links.get(notification_ts)
        if link_info:
            logging.info(f"Found thread link for notification_ts: {notification_ts}")
        return link_info
