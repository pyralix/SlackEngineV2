# deduplication.py
"""
Deduplication tools for Slack event handlers.
"""

from functools import wraps
from cachetools import LRUCache
from asyncio import Lock
import logging

class DuplicateEventError(Exception):
    """Raised when an event was already processed."""
    pass

class SlackEventDeduplicator:
    """
    Deduplicates event keys using LRUCache (thread-safe for asyncio).
    """
    def __init__(self, max_events=1000):
        self._cache = LRUCache(maxsize=max_events)
        self._lock = Lock()
        self.logger = logging.getLogger("SlackEventDeduplicator")

    async def is_duplicate(self, dedup_key: str) -> bool:
        async with self._lock:
            if dedup_key in self._cache:
                self.logger.warning(f"Duplicate deduplication key seen: {dedup_key}")
                return True
            self._cache[dedup_key] = True
            return False

DEDUPLICATOR = SlackEventDeduplicator()

def deduplicate_event(
    event_id_path=("event_id",),
    client_msg_id_key="client_msg_id"
):
    """
    Deduplicates based on event_id, or (fallback) client_msg_id at the top-level.
    """
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            logger = logging.getLogger("SlackEventDedupWrapper")

            event = kwargs.get("event") or kwargs.get("payload")
            if event is None:
                if args and hasattr(args[0], "__class__") and len(args) > 1:
                    event = args[1]
                elif args:
                    event = args[0]
            
            if not isinstance(event, dict):
                logger.debug("Could not extract event dict for deduplication.")
                return await fn(*args, **kwargs)

            eid = event
            for key in event_id_path:
                if not isinstance(eid, dict):
                    eid = None
                    break
                eid = eid.get(key)

            if eid:
                dedup_key = f"event_id:{eid}"
                label = "event_id"
            else:
                cid = event.get(client_msg_id_key)
                if cid:
                    dedup_key = f"client_msg_id:{cid}"
                    label = "client_msg_id"
                else:
                    # This event has no ID to deduplicate on, so we must let it pass.
                    # This is expected for some event types.
                    return await fn(*args, **kwargs)

            if await DEDUPLICATOR.is_duplicate(dedup_key):
                logger.warning(f"Duplicate event detected by {label}: {dedup_key}. Skipping handler.")
                return  # Skip duplicate
            
            return await fn(*args, **kwargs)
        return wrapper
    return decorator
