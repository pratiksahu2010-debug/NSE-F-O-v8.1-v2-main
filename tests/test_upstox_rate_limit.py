import time
import threading
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from data.errors import RateLimited
from data.upstox_client import UpstoxProvider
from utils.ratelimit import RateLimiter


class FakeResponse:
    def __init__(self, status_code, data=None, headers=None):
        self.status_code = status_code
        self._data = data or {}
        self.headers = headers or {}
        self.text = "rate limited"

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class FakeLimiter:
    def __init__(self):
        self.acquires = 0
        self.cooldowns = []

    def acquire(self):
        self.acquires += 1

    def defer(self, seconds):
        self.cooldowns.append(seconds)


class UpstoxRateLimitTests(unittest.TestCase):
    def provider(self, responses):
        provider = UpstoxProvider(token_fn=lambda: "test-token", limiter=FakeLimiter())
        provider.s = FakeSession(responses)
        return provider

    def _clock_patches(self):
        clock = [100.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += float(seconds)

        return patch("data.upstox_client.time.monotonic", side_effect=monotonic), patch(
            "data.upstox_client.time.sleep", side_effect=sleep
        )

    def test_429_is_retried_and_applies_shared_cooldown(self):
        provider = self.provider([
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(200, {"status": "success", "data": {"candles": []}}),
        ])
        mono, sleeper = self._clock_patches()
        with mono, sleeper:
            result = provider._get("/v3/historical-candle/test/minutes/5")
        self.assertEqual(result, {"candles": []})
        self.assertEqual(provider.s.calls, 2)
        self.assertEqual(provider.limiter.cooldowns, [15.0])

    def test_retry_after_header_longer_than_default_is_respected(self):
        provider = self.provider([
            FakeResponse(429, headers={"Retry-After": "25"}),
            FakeResponse(200, {"status": "success", "data": {}}),
        ])
        mono, sleeper = self._clock_patches()
        with mono, sleeper:
            provider._get("/test")
        self.assertEqual(provider.limiter.cooldowns, [25.0])

    def test_exhausted_429_retries_raise_rate_limited(self):
        provider = self.provider([FakeResponse(429) for _ in range(3)])
        mono, sleeper = self._clock_patches()
        with mono, sleeper:
            with self.assertRaises(RateLimited):
                provider._get("/test")
            with self.assertRaises(RateLimited):
                provider._get("/another-symbol")
        self.assertEqual(provider.s.calls, 3)  # two retries, then the open circuit prevents another REST call
        self.assertEqual(len(provider.limiter.cooldowns), 3)

    def test_rate_limiter_shared_defer_blocks_next_acquire(self):
        limiter = RateLimiter(100.0)
        limiter.defer(0.02)
        started = time.monotonic()
        limiter.acquire()
        self.assertGreaterEqual(time.monotonic() - started, 0.015)

    def test_defer_can_extend_cooldown_while_another_thread_waits(self):
        limiter = RateLimiter(100.0)
        limiter.defer(0.04)
        started = time.monotonic()
        worker = threading.Thread(target=limiter.acquire)
        worker.start()
        time.sleep(0.01)
        limiter.defer(0.08)
        worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(time.monotonic() - started, 0.075)


if __name__ == "__main__":
    unittest.main()
