"""Upstox instrument master -> universe, expiries and contract resolution.

Everything (lot size, tick size, expiries, strikes) comes from the master, nothing is hardcoded.
"""
import gzip
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from config import cfg
from models import Contract, Underlying
from utils.market_hours import IST

log = logging.getLogger("instruments")
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
DOWNLOAD_TIMEOUT = (10, 20)  # connect timeout, per-read timeout
DOWNLOAD_ATTEMPTS = 3
PROGRESS_BYTES = 5 * 1024 * 1024


def _ms_to_date(ms) -> Optional[date]:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, IST).date()
    except Exception:
        return None


_KEEP = ("segment", "underlying_symbol", "name", "expiry", "instrument_type", "underlying_key", "underlying_type",
         "strike_price", "instrument_key", "trading_symbol", "lot_size", "tick_size")


def iter_json_array(fp, chunk: int = 1 << 20):
    """Yield objects from a large top-level JSON array without loading it all into RAM."""
    dec, buf, started = json.JSONDecoder(), "", False
    while True:
        data = fp.read(chunk)
        buf += data
        i = 0
        while True:
            while i < len(buf) and buf[i] in " \t\r\n,":
                i += 1
            if not started:
                if i < len(buf) and buf[i] == "[":
                    started, i = True, i + 1
                    continue
                if i < len(buf):
                    raise ValueError("Instrument master is not a top-level JSON array")
                break
            if i >= len(buf):
                break
            if buf[i] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                if data:
                    break
                raise
            yield obj
            i = end
        buf = buf[i:]
        if not data:
            raise ValueError("Unexpected end of instrument master JSON")


def _latest_cache(cache_dir: str, exclude: str = "") -> Optional[str]:
    """Find a previously downloaded master so a temporary network outage needn't block startup."""
    candidates = []
    for name in os.listdir(cache_dir):
        if not name.startswith("NSE_master_") or not name.endswith(".json.gz") or name == exclude:
            continue
        path = os.path.join(cache_dir, name)
        try:
            with gzip.open(path, "rb") as f:
                f.read(2)  # ensure it is at least readable as gzip
            candidates.append((os.path.getmtime(path), path))
        except Exception:
            log.warning("Ignoring invalid cached instrument master: %s", name)
    return max(candidates)[1] if candidates else None


def download_master(cache_dir: str = None, force: bool = False) -> list:
    """Download and parse NSE instrument master with bounded network waits and progress logs."""
    cache_dir = cache_dir or cfg.DATA_DIR
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"NSE_master_{date.today().isoformat()}.json.gz")
    if force or not os.path.exists(path):
        log.info("Instrument master download started | url=%s | timeout=%ss read/%ss connect | attempts=%s",
                 MASTER_URL, DOWNLOAD_TIMEOUT[1], DOWNLOAD_TIMEOUT[0], DOWNLOAD_ATTEMPTS)
        tmp = path + ".part"
        downloaded = False
        last_error = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            started = time.monotonic()
            size = 0
            try:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                with requests.get(MASTER_URL, timeout=DOWNLOAD_TIMEOUT, stream=True,
                                  headers={"User-Agent": "NSESignalBot/1.0", "Accept-Encoding": "identity"}) as r:
                    log.info("Instrument master HTTP response | status=%s | content_length=%s | attempt=%s/%s",
                             r.status_code, r.headers.get("Content-Length", "unknown"), attempt, DOWNLOAD_ATTEMPTS)
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for part in r.iter_content(chunk_size=256 * 1024):
                            if not part:
                                continue
                            f.write(part)
                            size += len(part)
                            if size and size % PROGRESS_BYTES < len(part):
                                log.info("Instrument master download progress | %.1f MB | %.1f sec",
                                         size / (1024 * 1024), time.monotonic() - started)
                if size < 1024:
                    raise ValueError(f"Downloaded instrument master is suspiciously small ({size} bytes)")
                with gzip.open(tmp, "rb") as f:
                    if not f.read(2):
                        raise ValueError("Downloaded gzip master is empty")
                os.replace(tmp, path)
                log.info("Instrument master download complete | %.1f MB | %.1f sec",
                         size / (1024 * 1024), time.monotonic() - started)
                downloaded = True
                break
            except Exception as exc:
                last_error = exc
                log.warning("Instrument master download attempt %s/%s failed after %.1f sec: %s",
                            attempt, DOWNLOAD_ATTEMPTS, time.monotonic() - started, exc)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt < DOWNLOAD_ATTEMPTS:
                    time.sleep(min(2 * attempt, 5))
        if not downloaded:
            cached = _latest_cache(cache_dir, exclude=os.path.basename(path))
            if cached:
                log.warning("Using previously cached instrument master after download failure | file=%s | error=%s",
                            os.path.basename(cached), last_error)
                path = cached
            else:
                raise RuntimeError(f"Could not download Upstox instrument master after {DOWNLOAD_ATTEMPTS} attempts; no valid cache available: {last_error}") from last_error

    rows = []
    parsed = 0
    parse_started = time.monotonic()
    log.info("Instrument master parsing started | file=%s | compressed_bytes=%s", os.path.basename(path), os.path.getsize(path))
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for row in iter_json_array(f):
            parsed += 1
            if row.get("segment") == "NSE_FO":
                rows.append({k: row[k] for k in _KEEP if k in row})
            if parsed % 100000 == 0:
                log.info("Instrument master parsing progress | rows=%s | NSE_FO=%s | %.1f sec",
                         parsed, len(rows), time.monotonic() - parse_started)
    if not rows:
        raise ValueError(f"Instrument master parsed {parsed} rows but contained no NSE_FO instruments")
    log.info("Instrument master parsing complete | total_rows=%s | NSE_FO_rows=%s | %.1f sec",
             parsed, len(rows), time.monotonic() - parse_started)
    # Retain only the active file after a successful fresh download; stale fallback is retained on disk.
    if path == os.path.join(cache_dir, f"NSE_master_{date.today().isoformat()}.json.gz"):
        for old in os.listdir(cache_dir):
            if old.startswith("NSE_master_") and old.endswith(".json.gz") and old != os.path.basename(path):
                try:
                    os.remove(os.path.join(cache_dir, old))
                except OSError:
                    pass
    return rows


class Master:
    def __init__(self, rows: list):
        self.fo = [r for r in rows if r.get("segment") == "NSE_FO"]
        self.opts: Dict[tuple, dict] = {}
        self.expiries: Dict[str, List[date]] = defaultdict(list)
        self.futs: Dict[str, List[tuple]] = defaultdict(list)
        self._strikes: Dict[tuple, set] = defaultdict(set)
        self.spot_key: Dict[str, str] = {}
        self.kind: Dict[str, str] = {}
        for r in self.fo:
            name = (r.get("underlying_symbol") or r.get("name") or "").strip()
            if not name:
                continue
            exp = _ms_to_date(r.get("expiry"))
            if exp is None:
                continue
            it = r.get("instrument_type")
            if r.get("underlying_key"):
                self.spot_key.setdefault(name, r["underlying_key"])
            self.kind.setdefault(name, "INDEX" if str(r.get("underlying_type", "")).upper() == "INDEX" else "STOCK")
            if it in ("CE", "PE"):
                self.opts[(name, exp, float(r.get("strike_price", 0)), it)] = r
                self._strikes[(name, exp)].add(float(r.get("strike_price", 0)))
                if exp not in self.expiries[name]:
                    self.expiries[name].append(exp)
            elif it == "FUT":
                self.futs[name].append((exp, r))
        for n in self.expiries:
            self.expiries[n].sort()
        for n in self.futs:
            self.futs[n].sort(key=lambda x: x[0])

    # ---- lookups ----
    def names(self):
        return list(self.futs.keys())

    def current_future(self, name: str, today: date) -> Optional[dict]:
        for exp, r in self.futs.get(name, []):
            if exp >= today:
                return r
        return None

    def strike_step(self, name: str, expiry: date) -> float:
        ks = sorted(self._strikes.get((name, expiry), ()))
        diffs = sorted({round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a})
        # most common small step near the middle of the chain
        if len(ks) > 4:
            mid = len(ks) // 2
            d = ks[mid + 1] - ks[mid]
            if d > 0:
                return d
        return diffs[0] if diffs else 0.0

    def option(self, name: str, expiry: date, strike: float, opt_type: str) -> Optional[Contract]:
        r = self.opts.get((name, expiry, float(strike), opt_type))
        if not r:
            return None
        return Contract(underlying=name, expiry=expiry, strike=float(strike), opt_type=opt_type,
                        instrument_key=r["instrument_key"], trading_symbol=r.get("trading_symbol", ""),
                        lot_size=int(r.get("lot_size") or 0), tick_size=float(r.get("tick_size") or 0.05) / (100 if float(r.get("tick_size") or 0) >= 1 else 1))


def group_of(name: str) -> str:
    for g, members in cfg.GROUPS.items():
        if name in members:
            return g
    return ""


def build_underlyings(master: Master, today: date, stocks: Optional[List[str]] = None) -> List[Underlying]:
    """ALL F&O indices in the master + (optionally filtered) stocks. `stocks=None` returns all F&O stocks."""
    out = []
    for name in master.names():
        kind = master.kind.get(name, "STOCK")
        if kind == "STOCK" and stocks is not None and name not in stocks:
            continue
        fut = master.current_future(name, today)
        if not fut or name not in master.spot_key:
            continue
        exps = [e for e in master.expiries.get(name, []) if e >= today]
        step = master.strike_step(name, exps[0]) if exps else 0.0
        out.append(Underlying(name=name, kind=kind, spot_key=master.spot_key[name], fut_key=fut["instrument_key"],
                              lot_size=int(fut.get("lot_size") or 0), strike_step=step, group=group_of(name)))
    return out
