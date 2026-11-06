"""Upstox REST client (primary provider)."""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List
from urllib.parse import quote

import requests

from auth import tokens
from config import cfg
from data.errors import AuthError, DataUnavailable, ProviderError, RateLimited
from models import Candle, ChainSnapshot, Contract, OptionQuote, Underlying
from utils.market_hours import IST, now
from utils.ratelimit import RateLimiter

log = logging.getLogger("upstox")
BASE = "https://api.upstox.com"


def get_token() -> str:
    """Current Upstox token (analytics token / webhook token / file / env), resolved on every call."""
    return tokens.manager.upstox_token()


def _parse_candles(raw: list) -> List[Candle]:
    out = []
    for r in raw:
        try:
            ts = datetime.fromisoformat(r[0])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            out.append(Candle(ts.astimezone(IST), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                              float(r[5] or 0), float(r[6] or 0) if len(r) > 6 else 0.0))
        except Exception:
            continue
    out.sort(key=lambda c: c.ts)
    return out


class UpstoxProvider:
    name = "upstox"

    def __init__(self, ws=None, token_fn=get_token, limiter=None):
        self.token_fn = token_fn
        self.limiter = limiter or RateLimiter(cfg.UPSTOX_RPS)
        self.s = requests.Session()
        self.ws = ws
        self._rate_block_lock = threading.Lock()
        self._rate_blocked_until = 0.0

    # ---- transport ----
    def _get(self, path: str, params: dict = None, _retry: bool = True):
        tok = self.token_fn()
        if not tok:
            raise AuthError("Upstox token unavailable: configure a valid Upstox access token")
        max_rate_retries = max(0, int(getattr(cfg, "UPSTOX_429_RETRIES", 2)))
        for attempt in range(max_rate_retries + 1):
            with self._rate_block_lock:
                blocked_for = self._rate_blocked_until - time.monotonic()
            if blocked_for > 0:
                raise RateLimited(f"upstox shared rate-limit circuit open for {blocked_for:.1f}s")
            self.limiter.acquire()
            try:
                r = self.s.get(BASE + path, params=params, timeout=10,
                               headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
            except requests.RequestException as e:
                raise ProviderError(f"upstox network: {e}") from e
            if r.status_code in (401, 403):
                if _retry and tokens.manager.on_auth_error("upstox", rejected_token=tok) and self.token_fn() != tok:
                    return self._get(path, params, _retry=False)
                raise AuthError(f"upstox auth {r.status_code}")
            if r.status_code == 429:
                retry_header = (getattr(r, "headers", {}) or {}).get("Retry-After")
                try:
                    retry_after = max(0.0, float(retry_header)) if retry_header is not None else 0.0
                except (TypeError, ValueError):
                    retry_after = 0.0
                base_delay = max(1.0, float(getattr(cfg, "UPSTOX_429_COOLDOWN_SEC", 15.0)))
                delay = max(retry_after, base_delay * (2 ** attempt))
                with self._rate_block_lock:
                    self._rate_blocked_until = max(self._rate_blocked_until, time.monotonic() + delay)
                self.limiter.defer(delay)
                if attempt < max_rate_retries:
                    log.warning("Upstox rate limited | endpoint=%s | retry=%d/%d | shared_cooldown=%.1fs",
                                path.split("?")[0], attempt + 1, max_rate_retries, delay)
                    # Only the request that received 429 waits to retry. Other
                    # queued symbols fail fast while the shared circuit is open.
                    time.sleep(delay)
                    continue
                raise RateLimited(f"upstox 429 after {max_rate_retries + 1} attempts; shared cooldown {delay:.1f}s")
            if r.status_code >= 500:
                raise ProviderError(f"upstox {r.status_code}")
            if r.status_code >= 400:
                raise DataUnavailable(f"upstox {r.status_code}: {r.text[:200]}")
            j = r.json()
            if j.get("status") != "success":
                raise DataUnavailable(str(j)[:200])
            return j.get("data")
        raise RateLimited("upstox 429 retry budget exhausted")

    # ---- candles ----
    def _candles(self, key: str, path_tail: str) -> List[Candle]:
        data = self._get(f"/v3/historical-candle/{quote(key, safe='')}/{path_tail}")
        return _parse_candles((data or {}).get("candles", []))

    def _candles_intraday_key(self, key: str) -> List[Candle]:
        data = self._get(f"/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/{cfg.SCAN_MINUTES}")
        return _parse_candles((data or {}).get("candles", []))

    def spot_intraday(self, und: Underlying) -> List[Candle]:
        return self._candles_intraday_key(und.spot_key)

    def fut_intraday(self, und: Underlying) -> List[Candle]:
        return self._candles_intraday_key(und.fut_key)

    def history(self, und: Underlying, days: int) -> List[Candle]:
        to = now().date() - timedelta(days=1)
        frm = to - timedelta(days=days + 4)  # calendar padding for weekends/holidays
        c = self._candles(und.spot_key, f"minutes/{cfg.SCAN_MINUTES}/{to.isoformat()}/{frm.isoformat()}")
        # keep only the last `days` trading days
        dates = sorted({x.ts.date() for x in c})[-days:]
        return [x for x in c if x.ts.date() in dates]

    def daily(self, und: Underlying, days: int = 30) -> List[Candle]:
        to = now().date()
        frm = to - timedelta(days=days + 10)
        return self._candles(und.spot_key, f"days/1/{to.isoformat()}/{frm.isoformat()}")

    def vix_intraday(self) -> List[Candle]:
        return self._candles_intraday_key(cfg.UPSTOX_VIX_KEY)

    # ---- option chain ----
    def option_chain(self, und: Underlying, expiry: date) -> ChainSnapshot:
        data = self._get("/v2/option/chain", {"instrument_key": und.spot_key, "expiry_date": expiry.isoformat()})
        if not data:
            raise DataUnavailable("empty option chain")
        rows, spot, pcr_vals = {}, 0.0, []
        for item in data:
            strike = float(item.get("strike_price", 0))
            spot = float(item.get("underlying_spot_price") or spot)
            for side, t in (("call_options", "CE"), ("put_options", "PE")):
                o = item.get(side) or {}
                md, gk = o.get("market_data") or {}, o.get("option_greeks") or {}
                rows[(strike, t)] = OptionQuote(
                    strike=strike, opt_type=t, key=o.get("instrument_key", ""),
                    ltp=float(md.get("ltp") or 0), bid=float(md.get("bid_price") or 0), ask=float(md.get("ask_price") or 0),
                    bid_qty=float(md.get("bid_qty") or 0), ask_qty=float(md.get("ask_qty") or 0),
                    oi=float(md.get("oi") or 0), prev_oi=float(md.get("prev_oi") or 0), volume=float(md.get("volume") or 0),
                    iv=float(gk.get("iv") or 0), delta=float(gk.get("delta") or 0), theta=float(gk.get("theta") or 0))
        snap = ChainSnapshot(underlying=und.name, expiry=expiry, spot=spot, pcr=0.0, rows=rows, ts=now())
        snap.pcr = snap.calc_pcr()
        return snap

    # ---- premiums (REST fallback for the tracker) ----
    def premiums(self, contracts: List[Contract], unds=None) -> Dict[str, OptionQuote]:
        if not contracts:
            return {}
        keys = ",".join(c.instrument_key for c in contracts)
        data = self._get("/v2/market-quote/quotes", {"instrument_key": keys}) or {}
        out = {}
        by_key = {c.instrument_key: c for c in contracts}
        for _, q in data.items():
            k = q.get("instrument_token")
            c = by_key.get(k)
            if not c:
                continue
            depth = q.get("depth") or {}
            bid = (depth.get("buy") or [{}])[0].get("price", 0) or 0
            ask = (depth.get("sell") or [{}])[0].get("price", 0) or 0
            out[k] = OptionQuote(strike=c.strike, opt_type=c.opt_type, key=k, ltp=float(q.get("last_price") or 0),
                                 bid=float(bid), ask=float(ask), oi=float(q.get("oi") or 0), volume=float(q.get("volume") or 0))
        return out

    def spot_ltp(self, und: Underlying) -> float:
        data = self._get("/v2/market-quote/ltp", {"instrument_key": und.spot_key}) or {}
        for _, q in data.items():
            return float(q.get("last_price") or 0)
        raise DataUnavailable("no ltp")

    def ping(self) -> bool:
        data = self._get("/v2/market-quote/ltp", {"instrument_key": "NSE_INDEX|Nifty 50"})
        return bool(data)
