import time

class SessionManager:
    def __init__(self, ttl_minutes=30):
        self.sessions = {}  # (channel, thread) → session_id
        self.last_used = {}

    def get_or_create_session(self, channel, thread_ts):
        key = (channel, thread_ts)
        if key not in self.sessions:
            self.sessions[key] = f"session-{int(time.time())}"
        return self.sessions[key]

    def update_last_used(self, channel, thread_ts):
        self.last_used[(channel, thread_ts)] = time.time()

    def purge_old(self, cutoff=1800):
        now = time.time()
        for k, t in list(self.last_used.items()):
            if now - t > cutoff:
                self.sessions.pop(k, None)
                self.last_used.pop(k, None)
