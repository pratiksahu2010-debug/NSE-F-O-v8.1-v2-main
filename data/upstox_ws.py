"""Upstox Market Data Feed V3 websocket (primary live feed for the tracker).

Runs in its own thread/event loop. Exposes get(key) -> Tick and is_stale().
Messages are decoded by data/feed_decoder.py (no protoc needed). If `websockets` is missing, ws.enabled is False and
the bot uses REST polling.
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Dict, Iterable, Optional

import requests

from config import cfg
from data import feed_decoder
from data.feed_decoder import Tick  # noqa: F401  (re-exported)
from data.errors import AuthError
from auth import tokens
from data.upstox_client import get_token

log = logging.getLogger("upstox.ws")
AUTH_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"


class UpstoxWS:
    def __init__(self, token_fn=get_token, mode: str = "full"):
        self.token_fn, self.mode = token_fn, mode
        try:
            import websockets  # noqa: F401
            self.enabled = True
        except ImportError:
            log.warning("`websockets` not installed; websocket disabled, REST polling will be used")
            self.enabled = False
        self.keys = set()
        self.ticks: Dict[str, Tick] = {}
        self.last_msg = 0.0
        self.connected = False
        self._loop = None
        self._ws = None
        self._stop = False
        self._lock = threading.RLock()
        self._thread = None
        self._connected_once = False
        self._connected_at = None
        self._last_disconnect_at = None
        self._last_error = None
        self._reconnect_count = 0
        self._last_tick_by_key = {}
        self._sent_keys = set()
        self._subscription_send_errors = 0
        self._last_subscription_error = None
        self._last_decode_error = None
        self.on_tick = None  # optional non-blocking consumer callback

    # ---- public ----
    def start(self):
        if not self.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="upstox-ws", daemon=True)
            self._thread.start()

    def _schedule(self, coro):
        """Submit a coroutine only while the websocket loop is alive; shutdown races are benign."""
        loop = self._loop
        if loop is None or not loop.is_running() or self._stop:
            if hasattr(coro, "close"):
                coro.close()
            return False
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return True
        except RuntimeError:
            if hasattr(coro, "close"):
                coro.close()
            return False

    def stop(self, timeout: float = 5.0):
        # Close the socket before setting the stop flag: _schedule deliberately
        # rejects new work once shutdown begins.
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except RuntimeError:
                log.debug("websocket loop already stopping")
        self._stop = True
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        if thread and not thread.is_alive():
            self.connected = False

    def reconnect(self, *_):
        """Drop the socket so the loop re-authorizes with the (new) token."""
        ws = self._ws
        if ws is not None:
            self._schedule(ws.close())

    def subscribe(self, keys: Iterable[str]):
        with self._lock:
            new = set(keys) - self.keys
            self.keys |= new
        if new:
            self._send_threadsafe("sub", list(new))

    def unsubscribe(self, keys: Iterable[str]):
        with self._lock:
            gone = set(keys) & self.keys
            self.keys -= gone
        if gone:
            self._send_threadsafe("unsub", list(gone))

    def get(self, key: str) -> Optional[Tick]:
        with self._lock:
            return self.ticks.get(key)

    def tick_age(self, key: str) -> Optional[float]:
        with self._lock:
            stamp = self._last_tick_by_key.get(key)
        return round(time.time() - stamp, 3) if stamp else None

    def is_stale(self, max_age: float = None) -> bool:
        max_age = max_age or cfg.WS_STALE_SEC
        return (not self.connected) or (time.time() - self.last_msg) > max_age

    def status(self) -> dict:
        with self._lock:
            expected = set(self.keys)
            seen = set(self._last_tick_by_key)
            sent = set(self._sent_keys)
            key_count = len(expected)
            tick_count = len(expected & seen)
            missing = sorted(expected - seen)
            ages = {
                key: round(time.time() - self._last_tick_by_key[key], 1)
                for key in sorted(expected & seen)
            }
            oldest_age = max(ages.values(), default=None)
            sent_count = len(expected & sent)
            coverage = round(100.0 * tick_count / key_count, 1) if key_count else 0.0
            state = (
                "disconnected" if not self.connected else
                "connected_pending_send" if sent_count == 0 and key_count else
                "connected_partial_send" if sent_count < key_count else
                "connected_sent"
            )
            return {
                "enabled": self.enabled, "connected": self.connected,
                "subscription_state": state,
                "subs": key_count, "expected_subscription_count": key_count,
                "expected_subscription_keys": sorted(expected),
                "subscription_sent_keys": sorted(expected & sent),
                "subscription_sent_count": sent_count,
                "subscription_send_errors": self._subscription_send_errors,
                "last_subscription_error": self._last_subscription_error,
                "instruments_seen": tick_count,
                "tick_coverage_pct": coverage,
                "missing_tick_keys": missing[:150],
                "per_key_tick_age_seconds": ages,
                "age_sec": round(time.time() - self.last_msg, 1) if self.last_msg else None,
                "connected_since": self._connected_at, "last_disconnect_at": self._last_disconnect_at,
                "reconnect_count": self._reconnect_count, "last_error": self._last_error,
                "last_decode_error": self._last_decode_error,
                "oldest_instrument_tick_age_sec": oldest_age,
            }

    # ---- internals ----
    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                log.debug("websocket loop cleanup failed", exc_info=True)
            self._loop.close()
            self.connected = False
            self._loop = None

    def _authorize(self) -> str:
        """Authorize feed; reject bad tokens and retry once only if a different token exists."""
        tok = self.token_fn()
        if not tok:
            raise AuthError("Upstox websocket token unavailable; REST polling may be used when configured")
        for attempt in range(2):
            try:
                r = requests.get(AUTH_URL, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"}, timeout=10)
            except requests.RequestException as e:
                raise RuntimeError(f"Upstox websocket authorization network error: {e}") from e
            if r.status_code in (401, 403):
                changed = tokens.manager.on_auth_error("upstox", rejected_token=tok)
                replacement = self.token_fn()
                if attempt == 0 and changed and replacement and replacement != tok:
                    tok = replacement
                    continue
                raise AuthError(f"Upstox websocket auth {r.status_code}; token rejected; check UPSTOX_ANALYTICS_TOKEN")
            if r.status_code >= 400:
                raise RuntimeError(f"Upstox websocket authorization HTTP {r.status_code}: {r.text[:160]}")
            try:
                payload = r.json()
                uri = payload["data"]["authorized_redirect_uri"]
            except (ValueError, KeyError, TypeError) as e:
                raise RuntimeError("Upstox websocket authorization response missing authorized_redirect_uri") from e
            if not uri:
                raise RuntimeError("Upstox websocket authorization returned an empty redirect URI")
            return uri
        raise AuthError("Upstox websocket authorization failed")

    async def _main(self):
        import websockets  # imported lazily so unit tests don't need it
        backoff = 1
        auth_backoff = 60
        while not self._stop:
            auth_failed = False
            try:
                uri = self._authorize()
                if self._stop:
                    break
                async with websockets.connect(uri, max_size=None, ping_interval=20) as ws:
                    self._ws, self.connected, backoff = ws, True, 1
                    # stop() can race with authorization/connect; close immediately
                    # if shutdown was requested before the socket became visible.
                    self._connected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    if self._connected_once:
                        self._reconnect_count += 1
                    self._connected_once = True
                    self._last_error = None
                    with self._lock:
                        self._sent_keys.clear()
                    log.info("Upstox websocket connected | reconnect_count=%d | expected_subscriptions=%d",
                             self._reconnect_count, len(self.keys))
                    if self._stop:
                        await ws.close()
                        break
                    with self._lock:
                        subscribe_keys = list(self.keys)
                    if subscribe_keys:
                        await self._send("sub", subscribe_keys)
                    async for msg in ws:
                        self.last_msg = time.time()
                        if isinstance(msg, (bytes, bytearray)):
                            try:
                                self._on_message(bytes(msg))
                            except Exception as e:
                                self._last_decode_error = f"{type(e).__name__}: {e}"[:240]
                                log.warning("Upstox websocket frame decode failed: %s", e)
            except AuthError as e:
                auth_failed = True
                self._last_error = f"{type(e).__name__}: {e}"
                log.warning("Upstox websocket authentication unavailable: %s. REST polling remains available; retrying auth in %ss.", e, auth_backoff)
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                log.warning("Upstox websocket error: %s", e)
            finally:
                if self.connected:
                    self._last_disconnect_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.connected, self._ws = False, None
                with self._lock:
                    self._sent_keys.clear()
            # A deliberate stop must not sit through the reconnect backoff;
            # otherwise a subsequent start() can be blocked by the old thread.
            if self._stop:
                break
            await asyncio.sleep(auth_backoff if auth_failed else backoff)
            if self._stop:
                break
            if auth_failed:
                backoff = 1
            else:
                backoff = min(backoff * 2, 30)

    async def _send(self, method: str, keys: list):
        if not self._ws:
            return
        for i in range(0, len(keys), 100):
            payload = {"guid": uuid.uuid4().hex[:20], "method": method,
                       "data": {"mode": self.mode, "instrumentKeys": keys[i:i + 100]}}
            await self._ws.send(json.dumps(payload).encode("utf-8"))
            with self._lock:
                if method == "sub":
                    self._sent_keys.update(keys[i:i + 100])
                    self._last_subscription_error = None
                elif method == "unsub":
                    self._sent_keys.difference_update(keys[i:i + 100])

    def _send_threadsafe(self, method: str, keys: list):
        if self._loop and self._ws and self._loop.is_running() and not self._stop:
            try:
                future = asyncio.run_coroutine_threadsafe(self._send(method, keys), self._loop)
                def _log_send_error(done):
                    try:
                        done.result()
                    except (asyncio.CancelledError, RuntimeError):
                        log.debug("websocket %s request cancelled during shutdown", method)
                    except Exception as exc:
                        with self._lock:
                            self._subscription_send_errors += 1
                            self._last_subscription_error = f"{type(exc).__name__}: {exc}"[:240]
                        log.warning("websocket %s request failed", method, exc_info=True)
                future.add_done_callback(_log_send_error)
            except RuntimeError:
                log.debug("websocket loop stopped before %s request", method)

    def _on_message(self, raw: bytes):
        now = time.time()
        for key, t in feed_decoder.decode(raw).items():
            t.ts = now
            with self._lock:
                old = self.ticks.get(key)
                self._last_tick_by_key[key] = now
            if old:  # keep last known fields the new message does not carry
                for f in ("bid", "ask", "oi", "volume", "iv", "delta"):
                    if getattr(t, f) == 0:
                        setattr(t, f, getattr(old, f))
            with self._lock:
                self.ticks[key] = t
            callback = self.on_tick
            if callback is not None:
                try:
                    callback(key, t, now)
                except Exception:
                    log.exception("live tick callback failed for %s", key)
