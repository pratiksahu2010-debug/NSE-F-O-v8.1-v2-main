"""Single-provider adapter for Upstox-only deployments.

Keeps the small call/health interface consumed by the bot, scanner and tracker,
without silently switching to another broker or data source.
"""
from __future__ import annotations
import logging
import time
from collections import defaultdict

log = logging.getLogger("upstox_only")


class UpstoxOnlyProvider:
    name = "upstox"

    def __init__(self, provider):
        self.provider = provider
        self.latency_ms = defaultdict(float)
        self.calls = defaultdict(int)
        self.failures = defaultdict(int)
        self.last_error = None

    @property
    def active_name(self):
        return self.name

    def call(self, method: str, *args, **kwargs):
        fn = getattr(self.provider, method, None)
        if fn is None:
            raise AttributeError(f"Upstox provider does not support {method}")
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            self.calls[method] += 1
            elapsed = (time.perf_counter() - started) * 1000.0
            # EWMA avoids a single slow response dominating operational diagnostics.
            old = self.latency_ms[method]
            self.latency_ms[method] = elapsed if old <= 0 else old * 0.8 + elapsed * 0.2
            self.last_error = None
            return result
        except Exception as exc:
            self.failures[method] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise

    def health(self):
        return {
            "active": self.name,
            "configured": bool(self.provider.token_fn()),
            "mode": "upstox_only_no_failover",
            "latency_ms": dict(self.latency_ms),
            "calls": dict(self.calls),
            "failures": dict(self.failures),
            "last_error": self.last_error,
        }
