import threading
import time


class RateLimiter:
    """Thread-safe request pacer with a shared cooldown after provider throttling."""

    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / max(float(rate_per_sec), 0.01)
        self._next = 0.0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def defer(self, seconds: float):
        """Pause every caller using this limiter for at least the given duration."""
        delay = max(0.0, float(seconds))
        if not delay:
            return
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + delay)

    def acquire(self):
        """Reserve one paced slot without holding the lock while sleeping.

        Re-check after every wait so a concurrent 429 can extend the shared
        cooldown immediately instead of waiting behind a sleeping caller.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                wait_until = max(self._next, self._blocked_until)
                wait = wait_until - now
                if wait <= 0:
                    self._next = now + self.interval
                    return
            time.sleep(wait)
