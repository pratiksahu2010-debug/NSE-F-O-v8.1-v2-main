"""Provider failover: Upstox primary, Dhan secondary.

Switch rules
  * N consecutive *provider* failures (network/5xx/auth/429) on the active provider -> switch + Telegram alert
  * while on the secondary, probe the primary every FAILBACK_PROBE_SEC; after FAILBACK_OK_COUNT good probes -> switch back
  * DataUnavailable (valid empty response) never counts as a failure
"""
import logging
import threading
import time
from typing import Callable, Optional

from config import cfg
from data.errors import DataUnavailable, NotSupported, ProviderError

log = logging.getLogger("failover")


class FailoverManager:
    def __init__(self, primary, secondary, alert: Optional[Callable[[str], None]] = None):
        self.providers = {primary.name: primary, secondary.name: secondary}
        self.primary, self.secondary = primary, secondary
        self.active = primary
        self.fails = {primary.name: 0, secondary.name: 0}
        self.alert = alert or (lambda m: None)
        self._lock = threading.Lock()
        self._last_probe = 0.0
        self._probe_ok = 0
        self.switches = []
        self.latency_ms: dict = {}

    @property
    def active_name(self) -> str:
        return self.active.name

    def _other(self, p):
        return self.secondary if p is self.primary else self.primary

    def _switch(self, to, why: str):
        if to is self.active:
            return
        old = self.active
        self.active = to
        self.fails = {k: 0 for k in self.fails}
        self._probe_ok = 0
        self.switches.append((time.time(), old.name, to.name, why))
        log.warning("Provider switch %s -> %s (%s)", old.name, to.name, why)
        try:
            self.alert(f"⚠️ Data provider switched: {old.name.upper()} → {to.name.upper()}\nReason: {why}")
        except Exception:
            pass

    def _record_fail(self, p, err):
        with self._lock:
            self.fails[p.name] += 1
            if p is self.active and self.fails[p.name] >= cfg.FAIL_THRESHOLD:
                other = self._other(p)
                if getattr(other, "configured", lambda: True)():
                    self._switch(other, f"{self.fails[p.name]} consecutive failures: {err}")
                elif self.fails[p.name] == cfg.FAIL_THRESHOLD:   # alert once; do not flip to an unusable provider
                    try:
                        self.alert(f"🛑 {p.name.upper()} failing ({err}) and {other.name.upper()} is not configured -> no failover possible")
                    except Exception:
                        pass

    def _record_ok(self, p):
        with self._lock:
            self.fails[p.name] = 0

    def _maybe_failback(self):
        if self.active is self.primary or time.time() - self._last_probe < cfg.FAILBACK_PROBE_SEC:
            return
        self._last_probe = time.time()
        try:
            ok = self.primary.ping()
        except Exception:
            ok = False
        with self._lock:
            self._probe_ok = self._probe_ok + 1 if ok else 0
            if self._probe_ok >= cfg.FAILBACK_OK_COUNT:
                self._switch(self.primary, "primary healthy again")

    def call(self, method: str, *args, **kwargs):
        self._maybe_failback()
        order = [self.active, self._other(self.active)]
        last = None
        for p in order:
            fn = getattr(p, method, None)
            if fn is None:
                continue
            try:
                t0 = time.perf_counter()
                res = fn(*args, **kwargs)
                self._record_latency(p.name, (time.perf_counter() - t0) * 1000.0)
                self._record_ok(p)
                return res
            except NotSupported:
                continue
            except DataUnavailable as e:
                last = e
                continue
            except Exception as e:
                last = e
                log.warning("%s.%s failed: %s", p.name, method, e)
                self._record_fail(p, e)
        raise last if last else ProviderError(f"{method}: no provider available")

    def _record_latency(self, name: str, ms: float):
        prev = self.latency_ms.get(name)
        self.latency_ms[name] = round(ms if prev is None else 0.8 * prev + 0.2 * ms, 1)   # EWMA per provider

    def health(self) -> dict:
        return {"active": self.active.name, "fails": dict(self.fails), "switches": len(self.switches),
                "latency_ms": dict(self.latency_ms)}
