"""Dhan REST client (failover provider). Same interface as UpstoxProvider.

IDs: Dhan uses its own security ids, resolved from Dhan's scrip master (cached daily).
"""
import csv
import io
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List

import requests

from auth import tokens
from config import cfg
from data.errors import AuthError, DataUnavailable, NotSupported, ProviderError, RateLimited
from models import Candle, ChainSnapshot, Contract, OptionQuote, Underlying
from utils.market_hours import IST, now
from utils.ratelimit import RateLimiter

log = logging.getLogger("dhan")
BASE = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
FALLBACK_INDEX_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27, "MIDCPNIFTY": 442, "NIFTYNXT50": 38}


class DhanMaster:
    """Loads the compact scrip master and resolves underlying -> security ids."""

    def __init__(self):
        self.eq: Dict[str, int] = {}
        self.idx: Dict[str, int] = dict(FALLBACK_INDEX_IDS)
        self.fut: Dict[str, List[tuple]] = {}
        self.loaded = False

    def load(self):
        os.makedirs(cfg.DATA_DIR, exist_ok=True)
        path = os.path.join(cfg.DATA_DIR, f"dhan_master_{date.today().isoformat()}.csv")
        if not os.path.exists(path):
            r = requests.get(MASTER_URL, timeout=90)
            r.raise_for_status()
            open(path, "wb").write(r.content)
            for f in os.listdir(cfg.DATA_DIR):
                if f.startswith("dhan_master_") and f != os.path.basename(path):
                    try:
                        os.remove(os.path.join(cfg.DATA_DIR, f))
                    except OSError:
                        pass
        with open(path, newline="", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                exch, seg = row.get("SEM_EXM_EXCH_ID"), row.get("SEM_SEGMENT")
                inst = row.get("SEM_INSTRUMENT_NAME")
                sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
                try:
                    sid = int(row.get("SEM_SMST_SECURITY_ID"))
                except (TypeError, ValueError):
                    continue
                if exch == "NSE" and seg == "E" and inst == "EQUITY":
                    self.eq.setdefault(sym, sid)
                elif exch == "NSE" and seg == "I":
                    self.idx[sym] = sid
                elif exch == "NSE" and seg == "D" and inst in ("FUTIDX", "FUTSTK"):
                    try:
                        exp = date.fromisoformat((row.get("SEM_EXPIRY_DATE") or "")[:10])
                    except ValueError:
                        continue
                    self.fut.setdefault(sym.split("-")[0], []).append((exp, sid, inst))
        for k in self.fut:
            self.fut[k].sort()
        self.loaded = True
        log.info("Dhan master loaded: %d equities, %d indices, %d fut underlyings", len(self.eq), len(self.idx), len(self.fut))

    def resolve(self, und: Underlying):
        if und.kind == "INDEX":
            und.dhan_id = self.idx.get(und.name, FALLBACK_INDEX_IDS.get(und.name, 0))
            und.dhan_seg = "IDX_I"
        else:
            und.dhan_id = self.eq.get(und.name, 0)
            und.dhan_seg = "NSE_EQ"

    def current_fut(self, name: str, today: date):
        for exp, sid, inst in self.fut.get(name, []):
            if exp >= today:
                return sid, inst
        return None


class DhanProvider:
    name = "dhan"

    def __init__(self, limiter=None):
        self.limiter = limiter or RateLimiter(cfg.DHAN_DATA_RPS)
        self._chain_lock = threading.Lock()
        self._last_chain = 0.0
        self.s = requests.Session()
        self.master = DhanMaster()

    def configured(self) -> bool:
        return tokens.manager.dhan_configured()

    def _headers(self):
        tok = tokens.manager.dhan_token()
        if not (cfg.DHAN_CLIENT_ID and tok):
            if cfg.DHAN_CLIENT_ID:
                tokens.manager.refresh_dhan()          # try to mint one (TOTP) before giving up
                tok = tokens.manager.dhan_token()
            if not (cfg.DHAN_CLIENT_ID and tok):
                raise AuthError("Dhan credentials missing")
        return {"access-token": tok, "client-id": cfg.DHAN_CLIENT_ID,
                "Content-Type": "application/json", "Accept": "application/json"}

    def _post(self, path: str, body: dict, chain: bool = False, _retry: bool = True):
        h = self._headers()
        if chain:   # option chain is limited to ~1 request / 3 seconds
            with self._chain_lock:
                wait = cfg.DHAN_CHAIN_INTERVAL_SEC - (time.monotonic() - self._last_chain)
                if wait > 0:
                    time.sleep(wait)
                self._last_chain = time.monotonic()
        else:
            self.limiter.acquire()
        try:
            r = self.s.post(BASE + path, json=body, headers=h, timeout=12)
        except requests.RequestException as e:
            raise ProviderError(f"dhan network: {e}") from e
        if r.status_code in (401, 403):
            if _retry and tokens.manager.on_auth_error("dhan"):
                return self._post(path, body, chain, _retry=False)   # new token minted: retry once
            raise AuthError(f"dhan auth {r.status_code}")
        if r.status_code == 429:
            raise RateLimited("dhan 429")
        if r.status_code >= 500:
            raise ProviderError(f"dhan {r.status_code}")
        if r.status_code >= 400:
            raise DataUnavailable(f"dhan {r.status_code}: {r.text[:200]}")
        return r.json()

    # ---- candles ----
    @staticmethod
    def _parse(j: dict) -> List[Candle]:
        ts = j.get("timestamp") or j.get("start_Time") or []
        o, h, l, c, v = (j.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        oi = j.get("open_interest") or [0] * len(ts)
        out = []
        for i in range(len(ts)):
            try:
                t = datetime.fromtimestamp(float(ts[i]), IST)
                out.append(Candle(t, float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(v[i]),
                                  float(oi[i]) if i < len(oi) else 0.0))
            except Exception:
                continue
        return out

    def _intraday(self, sec_id, seg, instrument, frm: datetime, to: datetime, oi=False) -> List[Candle]:
        body = {"securityId": str(sec_id), "exchangeSegment": seg, "instrument": instrument,
                "interval": str(cfg.SCAN_MINUTES), "fromDate": frm.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": to.strftime("%Y-%m-%d %H:%M:%S")}
        if oi:
            body["oi"] = True
        c = self._parse(self._post("/charts/intraday", body))
        if not c:
            raise DataUnavailable("dhan: no candles")
        return c

    def _spot_args(self, und: Underlying):
        if not und.dhan_id:
            self.master.resolve(und)
        if not und.dhan_id:
            raise DataUnavailable(f"no dhan id for {und.name}")
        return und.dhan_id, und.dhan_seg, ("INDEX" if und.kind == "INDEX" else "EQUITY")

    def spot_intraday(self, und: Underlying) -> List[Candle]:
        t = now()
        sid, seg, ins = self._spot_args(und)
        return self._intraday(sid, seg, ins, t.replace(hour=9, minute=15, second=0, microsecond=0), t)

    def history(self, und: Underlying, days: int) -> List[Candle]:
        t = now()
        sid, seg, ins = self._spot_args(und)
        c = self._intraday(sid, seg, ins, (t - timedelta(days=days + 4)).replace(hour=9, minute=15, second=0),
                           t.replace(hour=0, minute=0, second=0))
        dates = sorted({x.ts.date() for x in c})[-days:]
        return [x for x in c if x.ts.date() in dates]

    def daily(self, und: Underlying, days: int = 30) -> List[Candle]:
        sid, seg, ins = self._spot_args(und)
        t = now()
        j = self._post("/charts/historical", {"securityId": str(sid), "exchangeSegment": seg, "instrument": ins,
                                              "expiryCode": 0, "fromDate": (t - timedelta(days=days + 10)).strftime("%Y-%m-%d"),
                                              "toDate": t.strftime("%Y-%m-%d")})
        c = self._parse(j)
        if not c:
            raise DataUnavailable("dhan: no daily candles")
        return c

    def fut_intraday(self, und: Underlying) -> List[Candle]:
        if not self.master.loaded:
            raise NotSupported("dhan master not loaded")
        f = self.master.current_fut(und.name, now().date())
        if not f:
            raise DataUnavailable("no dhan future")
        sid, inst = f
        t = now()
        return self._intraday(sid, "NSE_FNO", inst, t.replace(hour=9, minute=15, second=0, microsecond=0), t, oi=True)

    def vix_intraday(self) -> List[Candle]:
        t = now()
        return self._intraday(cfg.DHAN_VIX_ID, "IDX_I", "INDEX", t.replace(hour=9, minute=15, second=0, microsecond=0), t)

    # ---- option chain ----
    def option_chain(self, und: Underlying, expiry: date) -> ChainSnapshot:
        sid, seg, _ = self._spot_args(und)
        j = self._post("/optionchain", {"UnderlyingScrip": sid, "UnderlyingSeg": seg, "Expiry": expiry.isoformat()}, chain=True)
        data = j.get("data") or {}
        oc = data.get("oc") or {}
        if not oc:
            raise DataUnavailable("dhan: empty chain")
        rows = {}
        for k, v in oc.items():
            strike = float(k)
            for side, t in (("ce", "CE"), ("pe", "PE")):
                o = v.get(side)
                if not o:
                    continue
                g = o.get("greeks") or {}
                rows[(strike, t)] = OptionQuote(
                    strike=strike, opt_type=t, key=f"DHAN:{und.name}:{expiry}:{strike}:{t}",
                    ltp=float(o.get("last_price") or 0), bid=float(o.get("top_bid_price") or 0), ask=float(o.get("top_ask_price") or 0),
                    bid_qty=float(o.get("top_bid_quantity") or 0), ask_qty=float(o.get("top_ask_quantity") or 0),
                    oi=float(o.get("oi") or 0), prev_oi=float(o.get("previous_oi") or 0), volume=float(o.get("volume") or 0),
                    iv=float(o.get("implied_volatility") or 0), delta=float(g.get("delta") or 0), theta=float(g.get("theta") or 0))
        snap = ChainSnapshot(underlying=und.name, expiry=expiry, spot=float(data.get("last_price") or 0), pcr=0.0, rows=rows, ts=now())
        snap.pcr = snap.calc_pcr()
        return snap

    def premiums(self, contracts: List[Contract], unds: Dict[str, Underlying] = None) -> Dict[str, OptionQuote]:
        """Dhan has no cheap per-contract quote without ids, so read the chain (1 call per underlying/expiry)."""
        unds = unds or {}
        out, seen = {}, {}
        for c in contracts:
            u = unds.get(c.underlying)
            if not u:
                continue
            k = (c.underlying, c.expiry)
            if k not in seen:
                seen[k] = self.option_chain(u, c.expiry)
            q = seen[k].rows.get((c.strike, c.opt_type))
            if q:
                out[c.instrument_key] = q
        return out

    def ping(self) -> bool:
        t = now()
        return bool(self._post("/marketfeed/ltp", {"IDX_I": [13]}))
