# deduplication.py
"""
Deduplication tools for Slack event handlers.

Provides a decorator to de-duplicate Slack Events API payloads using 'event_id',
and falls back to top-level 'client_msg_id' if needed.

Install:
    pip install cachetools
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
    Replace with Redis/Memcached for distributed use.
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

    Args:
        event_id_path: Tuple path in the event dict to find event_id.
        client_msg_id_key: Key for client_msg_id (default: 'client_msg_id', top-level).
    """
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            logger = logging.getLogger("SlackEventDedupWrapper")

            # Pull event dict from kwargs or args (works for self methods too)
            event = kwargs.get("event") or kwargs.get("payload")
            if event is None:
                if args and hasattr(args[0], "__class__") and len(args) > 1:
                    event = args[1]
                elif args:
                    event = args[0]
            if not isinstance(event, dict):
                logger.warning("Could not extract event dict for deduplication.")
                return await fn(*args, **kwargs)

            # Try to get event_id from path
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
                # Fallback: client_msg_id at top level
                cid = event.get(client_msg_id_key)
                if cid:
                    dedup_key = f"client_msg_id:{cid}"
                    label = "client_msg_id"
                else:
                    assistant_thread = event.get('assistant_thread')
                    tid = None
                    if assistant_thread is not None:
                        tid = assistant_thread.get('thread_ts')
                    if tid:
                        dedup_key = f"thread_id:{tid}"
                        label = "thread_id"
                    else:
                        ets = event.get('event_ts')
                        if ets:
                            dedup_key = f"event_ts:{ets}"
                            label = "event_ts"
                        else:
                            dedup_key = None
                            label = None

            if dedup_key:
                if await DEDUPLICATOR.is_duplicate(dedup_key):
                    logger.warning(f"Duplicate event detected by {label}: {dedup_key}. Skipping handler.")
                    return  # Skip duplicate
            else:
                logger.warning(
                    "No event_id or client_msg_id found in event dict; cannot deduplicate. Handler will run.")

            return await fn(*args, **kwargs)
        return wrapper
    return decorator
