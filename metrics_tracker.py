"""
A thread-safe, time series metrics tracker that logs events to a CSV file.
"""

import csv
import logging
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from config_loader import MetricsConfig


class MetricsCSVTracker:
    """
    Handles the logging of events to a CSV file for time series analysis.
    This class is thread-safe.
    """

    _HEADERS = [
        "timestamp",
        "event_type",
        "bot_name",
        "channel_id",
        "thread_ts",
        "time_saved_minutes",
        "sentiment_value",
    ]

    def __init__(self, config: MetricsConfig, bot_name: str):
        """
        Initializes the tracker.

        Args:
            config: The metrics configuration object.
            bot_name: The name of the bot instance for logging.
        """
        self.config = config
        self.bot_name = bot_name
        self.filepath = self.config.metrics_storage_path
        self._lock = Lock()
        self._ensure_file_exists()
        self.logger = logging.getLogger(__name__)
        self.logger.info(f"MetricsCSVTracker initialized for bot '{self.bot_name}' at {self.filepath}")

    def _ensure_file_exists(self):
        """Creates the CSV file with a header row if it doesn't exist."""
        with self._lock:
            if not os.path.exists(self.filepath):
                try:
                    with open(self.filepath, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(self._HEADERS)
                    logging.info(f"Created new metrics file with headers at {self.filepath}")
                except IOError as e:
                    logging.error(f"Failed to create metrics file at {self.filepath}: {e}")

    def log_event(
        self,
        event_type: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        time_saved_minutes: Optional[int] = None,
        sentiment_value: Optional[str] = None,
    ):
        """
        Logs a single event by appending a new row to the CSV file.

        Args:
            event_type: The name of the event (e.g., 'autonomous_response').
            channel_id: The Slack channel ID where the event occurred.
            thread_ts: The Slack thread timestamp related to the event.
            time_saved_minutes: The estimated time saved for this event.
            sentiment_value: The sentiment of a user reaction ('positive' or 'negative').
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        row = {
            "timestamp": timestamp,
            "event_type": event_type,
            "bot_name": self.bot_name,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "time_saved_minutes": time_saved_minutes,
            "sentiment_value": sentiment_value,
        }

        with self._lock:
            try:
                with open(self.filepath, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=self._HEADERS)
                    writer.writerow(row)
            except IOError as e:
                self.logger.error(f"Failed to write to metrics file {self.filepath}: {e}")
            except Exception as e:
                self.logger.error(f"An unexpected error occurred while logging metrics: {e}")
