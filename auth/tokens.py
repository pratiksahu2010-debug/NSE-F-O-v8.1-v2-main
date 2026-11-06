"""Automatic token management for Upstox (primary) and Dhan (failover).

Upstox
  1. Analytics Token (recommended): read-only, valid 1 year, covers market quotes, historical candles, option chain
     and the Market Data Feed V3 -- everything this signal-only bot uses. Paste it once; the bot warns on Telegram
     at 14/7/3/1 days before expiry.
  2. Optional daily flow (UPSTOX_API_KEY + UPSTOX_API_SECRET, no analytics token): the bot calls Upstox's
     "access token request"; you approve it on the Upstox app/WhatsApp and Upstox POSTs the token to
     /webhook/upstox-token/<secret> on this service. Needs one tap per day by design (Upstox rule).
  3. Legacy: UPSTOX_ACCESS_TOKEN env / UPSTOX_TOKEN_FILE.
Dhan
  * DHAN_CLIENT_ID + DHAN_PIN + DHAN_TOTP_SECRET -> the bot generates a new 24h token by itself (TOTP computed locally).
  * Otherwise a static DHAN_ACCESS_TOKEN is used and renewed through Dhan's RenewToken endpoint when close to expiry.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import requests

from config import cfg

log = logging.getLogger("tokens")
UPSTOX_REQ_URL = "https://api.upstox.com/v3/login/auth/token/request/{key}"
DHAN_GEN_URL = "https://auth.dhan.co/app/generateAccessToken"
DHAN_RENEW_URL = "https://api.dhan.co/v2/RenewToken"
EXPIRY_WARN_DAYS = (14, 7, 3, 1)


# ---------------- helpers ----------------
def totp(secret: str, t: float = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (SHA-1), stdlib only."""
    s = secret.replace(" ", "").replace("-", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    h = hmac.new(key, struct.pack(">Q", int((time.time() if t is None else t) // step)), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def jwt_exp(token: str) -> Optional[float]:
    """Unverified `exp` claim of a JWT (epoch seconds) or None."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def _ist_iso_to_epoch(s: str) -> Optional[float]:
    try:
        from utils.market_hours import IST
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=IST)).timestamp()
    except Exception:
        return None


class TokenManager:
    def __init__(self, alert: Callable[[str], object] = None, kv=None, kv_factory=None):
        self.alert = alert or (lambda m: None)
        self.kv = kv                                   # optional existing KV client (mainly tests)
        self.kv_factory = kv_factory                  # lazy isolated KV client factory for production
        self._kv_client = kv
        self._lock = threading.RLock()
        self._up_runtime = ("", None)                  # (token, exp) set by webhook
        # Tokens rejected by Upstox (401/403) are skipped for this process lifetime.
        # This prevents a stale persisted/webhook token from masking a valid fallback token.
        self._up_rejected = set()
        self._dhan = ("", None)                        # (token, exp)
        self._last_dhan_try = 0.0
        self._last_up_request = 0.0
        self._alerted: Dict[str, float] = {}
        self.on_change: Optional[Callable[[str], None]] = None
        self.last_error = {}
        self._persist_loaded = False
        self._persist_loading = False
        self._persist_restore_error = None
        self._persist_retry_after = 0.0

    def _get_kv(self):
        """Return the optional KV client, constructing an isolated client lazily."""
        with self._lock:
            if self._kv_client is None and self.kv_factory is not None:
                self._kv_client = self.kv_factory()
            return self._kv_client

    def _persist(self, key: str, token: str, exp: Optional[float]):
        """Persist refreshed runtime credentials in the configured DB so Render restarts don't discard them."""
        if not token:
            return
        try:
            kv = self._get_kv()
            if kv is None:
                return
            kv.kv_set(key, json.dumps({"token": token, "exp": exp}))
        except Exception:
            log.warning("could not persist refreshed %s credential", key)

    def _restore_persisted(self):
        """Restore optional webhook tokens off-loop using an isolated KV connection."""
        with self._lock:
            if self._persist_loaded or self._persist_loading or time.time() < self._persist_retry_after:
                return
            if self._kv_client is None and self.kv_factory is None:
                return
            self._persist_loading = True

        def _restore():
            failures = []
            restored = {}
            try:
                kv = self._get_kv()
                if kv is None:
                    return
                for key in ("runtime_upstox_token", "runtime_dhan_token"):
                    try:
                        raw = kv.kv_get(key)
                        item = json.loads(raw) if raw else {}
                        token, exp = item.get("token", ""), item.get("exp")
                        if token and (not exp or float(exp) > time.time() + 60):
                            restored[key] = (token, float(exp) if exp else jwt_exp(token))
                    except Exception as exc:
                        failures.append(f"{key}: {type(exc).__name__}: {exc}")
                        log.warning("could not restore persisted credential %s", key, exc_info=True)
                with self._lock:
                    # Never overwrite a token delivered by a webhook during the DB read.
                    if "runtime_upstox_token" in restored and not self._up_runtime[0]:
                        self._up_runtime = restored["runtime_upstox_token"]
                    if "runtime_dhan_token" in restored and not self._dhan[0]:
                        self._dhan = restored["runtime_dhan_token"]
                    if failures:
                        self._persist_restore_error = "; ".join(failures)[:180]
                        self._persist_retry_after = time.time() + 15.0
                    else:
                        self._persist_loaded = True
                        self._persist_restore_error = None
                        self._persist_retry_after = 0.0
            except Exception as exc:
                with self._lock:
                    self._persist_restore_error = f"{type(exc).__name__}: {exc}"[:180]
                    self._persist_retry_after = time.time() + 15.0
                log.warning("persisted token restore failed", exc_info=True)
            finally:
                with self._lock:
                    self._persist_loading = False

        try:
            threading.Thread(target=_restore, name="token-persist-restore", daemon=True).start()
        except Exception:
            with self._lock:
                self._persist_loading = False
            raise

    # ---------- alert dedupe ----------
    def _alert_once(self, key: str, msg: str, hours: float = 6.0):
        now = time.time()
        if now - self._alerted.get(key, 0) < hours * 3600:
            return
        self._alerted[key] = now
        try:
            self.alert(msg)
        except Exception:
            log.exception("alert failed")

    def _changed(self, which: str):
        if self.on_change:
            try:
                self.on_change(which)
            except Exception:
                log.exception("on_change failed")

    # ---------- Upstox ----------
    def _up_candidates(self):
        out = []
        if self._up_runtime[0]:
            out.append(("webhook", self._up_runtime[0], self._up_runtime[1]))
        f = cfg.UPSTOX_TOKEN_FILE
        if f and os.path.exists(f):
            try:
                t = open(f).read().strip()
                if t:
                    out.append(("file", t, jwt_exp(t)))
            except OSError:
                pass
        a = os.getenv("UPSTOX_ANALYTICS_TOKEN", cfg.UPSTOX_ANALYTICS_TOKEN)
        if a:
            exp = jwt_exp(a)
            if exp is None and cfg.UPSTOX_ANALYTICS_EXPIRES:
                try:
                    exp = datetime.fromisoformat(cfg.UPSTOX_ANALYTICS_EXPIRES).timestamp()
                except ValueError:
                    pass
            out.append(("analytics", a, exp))
        t = os.getenv("UPSTOX_ACCESS_TOKEN", cfg.UPSTOX_ACCESS_TOKEN)
        if t:
            out.append(("env", t, jwt_exp(t)))
        return out

    def upstox_source(self):
        """Return the best non-expired token not already rejected by Upstox."""
        c = self._up_candidates()
        now = time.time()
        with self._lock:
            c = [item for item in c if item[1] and item[1] not in self._up_rejected]
        for src, tok, exp in c:
            if exp is None or exp > now + 60:
                return src, tok, exp
        # Do not return an expired/rejected credential: callers can fail over cleanly.
        return ("none", "", None)

    def upstox_token(self) -> str:
        return self.upstox_source()[1]

    def set_upstox(self, token: str, exp: Optional[float], source: str = "webhook"):
        actual_exp = exp or jwt_exp(token)
        with self._lock:
            self._up_runtime = (token, actual_exp)
            self._up_rejected.discard(token)
        self._persist("runtime_upstox_token", token, actual_exp)
        log.info("Upstox token updated via %s", source)
        self._changed("upstox")

    def handle_upstox_webhook(self, payload: dict) -> bool:
        if not isinstance(payload, dict) or payload.get("message_type") != "access_token":
            return False
        if cfg.UPSTOX_API_KEY and payload.get("client_id") != cfg.UPSTOX_API_KEY:
            log.warning("Upstox webhook with unexpected client_id ignored")
            return False
        tok = payload.get("access_token")
        if not tok:
            return False
        try:
            exp = float(payload.get("expires_at")) / 1000.0
        except (TypeError, ValueError):
            exp = None
        self.set_upstox(tok, exp)
        self._alert_once("up-new", "🔑 Upstox token received and activated.", 0.01)
        return True

    def request_upstox_token(self, force: bool = False) -> bool:
        """Start Upstox's approve-on-phone flow. Rate-limited to once per 30 min."""
        if not (cfg.UPSTOX_API_KEY and cfg.UPSTOX_API_SECRET):
            return False
        if not force and time.time() - self._last_up_request < 1800:
            return False
        self._last_up_request = time.time()
        try:
            r = requests.post(UPSTOX_REQ_URL.format(key=cfg.UPSTOX_API_KEY), timeout=15,
                              headers={"Content-Type": "application/json", "Accept": "application/json"},
                              json={"client_secret": cfg.UPSTOX_API_SECRET})
            ok = r.ok and r.json().get("status") == "success"
            if ok:
                self._alert_once("up-req", "📲 Upstox token request sent. Approve it in the Upstox app / WhatsApp "
                                           "within a few minutes; the bot picks the token up automatically.", 1)
            else:
                self.last_error["upstox_request"] = r.text[:200]
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                errors = body.get("errors") or []
                notifier_error = any(
                    str(item.get("errorCode") or item.get("error_code") or "") == "UDAPI1123"
                    or "invalid notifier url" in str(item.get("message", "")).lower()
                    for item in errors if isinstance(item, dict)
                )
                if notifier_error:
                    message = ("⚠️ Upstox daily token request rejected: the notifier URL configured in your "
                               "Upstox Developer App is invalid. Correct that app setting, or remove "
                               "UPSTOX_API_KEY/UPSTOX_API_SECRET and use a valid UPSTOX_ANALYTICS_TOKEN.")
                    self._alert_once("up-notifier-url", message, 6)
                else:
                    self._alert_once("up-req-fail", f"⚠️ Upstox token request failed: {r.text[:200]}", 6)
            return ok
        except Exception as e:
            self.last_error["upstox_request"] = str(e)
            log.warning("upstox token request failed: %s", e)
            return False

    # ---------- Dhan ----------
    def dhan_token(self) -> str:
        with self._lock:
            tok = self._dhan[0]
        return tok or os.getenv("DHAN_ACCESS_TOKEN", cfg.DHAN_ACCESS_TOKEN)

    def dhan_exp(self) -> Optional[float]:
        with self._lock:
            if self._dhan[0]:
                return self._dhan[1]
        t = self.dhan_token()
        return jwt_exp(t) if t else None

    def dhan_configured(self) -> bool:
        return bool(cfg.DHAN_CLIENT_ID and (self.dhan_token() or (cfg.DHAN_PIN and cfg.DHAN_TOTP_SECRET)))

    def _dhan_generate(self) -> bool:
        for attempt in range(3):
            try:
                r = requests.post(DHAN_GEN_URL, timeout=15, params={
                    "dhanClientId": cfg.DHAN_CLIENT_ID, "pin": cfg.DHAN_PIN, "totp": totp(cfg.DHAN_TOTP_SECRET)})
                j = r.json() if r.content else {}
                tok = j.get("accessToken")
                if r.ok and tok:
                    exp = _ist_iso_to_epoch(j.get("expiryTime", "")) or (time.time() + 23 * 3600)
                    with self._lock:
                        self._dhan = (tok, exp)
                    self._persist("runtime_dhan_token", tok, exp)
                    log.info("Dhan token generated via TOTP (valid ~%.0fh)", (exp - time.time()) / 3600)
                    self._changed("dhan")
                    return True
                self.last_error["dhan_generate"] = str(j)[:200] or r.text[:200]
            except Exception as e:
                self.last_error["dhan_generate"] = str(e)
            if attempt < 2:
                time.sleep(32)            # a TOTP code can only be used once per 30s window
        self._alert_once("dhan-gen", f"⚠️ Dhan token generation failed: {self.last_error.get('dhan_generate')}", 6)
        return False

    def _dhan_renew(self) -> bool:
        tok = self.dhan_token()
        if not tok:
            return False
        try:
            r = requests.get(DHAN_RENEW_URL, timeout=15, headers={"access-token": tok, "dhanClientId": cfg.DHAN_CLIENT_ID})
            j = r.json() if r.content else {}
            new = j.get("accessToken") or j.get("token")
            if r.ok and new:
                exp = _ist_iso_to_epoch(j.get("expiryTime", "")) or (time.time() + 23 * 3600)
                with self._lock:
                    self._dhan = (new, exp)
                self._persist("runtime_dhan_token", new, exp)
                self._changed("dhan")
                return True
            self.last_error["dhan_renew"] = r.text[:200]
        except Exception as e:
            self.last_error["dhan_renew"] = str(e)
        return False

    def refresh_dhan(self, force: bool = False, min_remaining_h: float = 1.0) -> bool:
        if not cfg.DHAN_CLIENT_ID:
            return False
        exp = self.dhan_exp()
        remaining = (exp - time.time()) / 3600 if exp else (0 if not self.dhan_token() else None)
        if not force and remaining is not None and remaining > min_remaining_h:
            return True
        if time.time() - self._last_dhan_try < 120 and not force:
            return False
        self._last_dhan_try = time.time()
        if cfg.DHAN_PIN and cfg.DHAN_TOTP_SECRET:
            return self._dhan_generate()
        if self.dhan_token() and (remaining is None or remaining > 0):
            if self._dhan_renew():
                return True
        self._alert_once("dhan-manual", "⚠️ Dhan token is missing/expiring. Set DHAN_PIN + DHAN_TOTP_SECRET for auto-refresh "
                                        "or paste a new DHAN_ACCESS_TOKEN.", 12)
        return False

    # ---------- orchestration ----------
    def ensure_fresh(self, morning: bool = False) -> dict:
        """Cheap and idempotent: call from the loop, from /cron/tick and from bootstrap."""
        res = {}
        self._restore_persisted()
        try:
            src, tok, exp = self.upstox_source()
            now = time.time()
            if tok and (exp is None or exp > now):
                if src == "analytics" and exp:
                    days = (exp - now) / 86400
                    for n in EXPIRY_WARN_DAYS:
                        if days <= n:
                            self._alert_once(f"up-exp-{n}", f"⏳ Upstox Analytics Token expires in {days:.0f} day(s). Generate a new one "
                                                          "(Developer Apps -> Analytics) and update UPSTOX_ANALYTICS_TOKEN.", 20)
                            break
            else:   # missing or expired
                if not self.request_upstox_token():
                    self._alert_once("up-dead", "🛑 Upstox token is missing/expired. Set UPSTOX_ANALYTICS_TOKEN "
                                                "(1-year, read-only) or configure UPSTOX_API_KEY/SECRET.", 6)
            res["upstox"] = {"source": src, "days_left": round((exp - now) / 86400, 1) if exp else None, "ok": bool(tok) and (exp is None or exp > now)}
        except Exception:
            log.exception("upstox token check failed")
        # The live bot is deliberately Upstox-only; legacy Dhan token helpers are not invoked.
        return res

    def on_auth_error(self, provider: str, rejected_token: str = "") -> bool:
        """Handle 401/403; blacklist the rejected Upstox token and retry only with a different token."""
        if provider == "dhan":
            return self.refresh_dhan(force=time.time() - self._last_dhan_try > 120)
        if rejected_token:
            with self._lock:
                self._up_rejected.add(rejected_token)
        # If another configured token is valid, callers can immediately retry with it.
        replacement = self.upstox_token()
        if replacement and replacement != rejected_token:
            self._alert_once("up-token-fallback", "⚠️ Upstox rejected one token; trying another configured token.", 3)
            return True
        self._alert_once("up-401", "🛑 Upstox rejected its configured token (401/403). The rejected token is disabled for this process. "
                                   "Generate a fresh Upstox Analytics Token and update UPSTOX_ANALYTICS_TOKEN, or approve the configured daily token request.", 3)
        # Daily approval is optional and cannot refresh an Analytics token automatically.
        self.request_upstox_token()
        return False

    def status(self) -> dict:
        src, tok, exp = self.upstox_source()
        return {"upstox": {"source": src, "present": bool(tok), "days_left": round((exp - time.time()) / 86400, 1) if exp else None},
                "provider_mode": "upstox_only", "errors": dict(self.last_error)}


manager = TokenManager()
