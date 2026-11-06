"""Follows live signals: premium from the Upstox websocket (fresh ticks) else REST via the failover manager."""
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from config import cfg
from data.errors import DataUnavailable
from engine.outcomes import exit_fill_price, round_trip_cost
from models import Contract, Signal, Underlying
from telegram_service import format_update
from utils.market_hours import now as ist_now, past_exit_time

log = logging.getLogger("tracker")


class Tracker:
    def __init__(self, db, tg, fo, ws, unds_by_name: Dict[str, Underlying]):
        self.db, self.tg, self.fo, self.ws, self.unds = db, tg, fo, ws, unds_by_name
        self.active: Dict[str, tuple] = {}          # id -> (Signal, Contract)
        self._rest_cache: Dict[str, tuple] = {}     # key -> (ts, premium)
        self._spot_cache: Dict[str, tuple] = {}
        self._last_prem: Dict[str, float] = {}     # last premium actually seen per signal (for no-quote time exits)
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None

    # ---- registry ----
    def add(self, sig: Signal, contract: Contract):
        with self._lock:
            self.active[sig.id] = (sig, contract)
        if self.ws and self.ws.enabled:
            u = self.unds.get(sig.underlying)
            self.ws.subscribe([contract.instrument_key] + ([u.spot_key] if u else []))

    def signals(self):
        with self._lock:
            return [s for s, _ in self.active.values()]

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="tracker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop = True
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def _loop(self):
        while not self._stop:
            try:
                self.step(ist_now())
            except Exception:
                log.exception("tracker step failed")
            time.sleep(cfg.TRACK_INTERVAL_SEC)

    # ---- premium / spot ----
    def _ws_premium(self, c: Contract) -> Optional[float]:
        if self.ws and self.ws.enabled and not self.ws.is_stale():
            t = self.ws.get(c.instrument_key)
            if t and time.time() - t.ts < cfg.WS_STALE_SEC and t.ltp > 0:
                return t.ltp
        return None

    def _premium(self, c: Contract) -> Optional[float]:
        p = self._ws_premium(c)
        if p is not None:
            return p
        cached = self._rest_cache.get(c.instrument_key)
        if cached and time.time() - cached[0] < cfg.REST_PREMIUM_REFRESH_SEC:
            return cached[1]
        self._refresh_rest([c])
        cached = self._rest_cache.get(c.instrument_key)
        return cached[1] if cached and time.time() - cached[0] < cfg.REST_PREMIUM_REFRESH_SEC else None

    def _refresh_rest(self, contracts):
        """One REST call for all contracts that need it (not one call per contract)."""
        if not contracts:
            return
        try:
            res = self.fo.call("premiums", contracts, unds=self.unds)
            now = time.time()
            for c in contracts:
                q = res.get(c.instrument_key)
                if q and q.ltp > 0:
                    self._rest_cache[c.instrument_key] = (now, q.ltp)
        except DataUnavailable:
            pass
        except Exception as e:
            log.warning("premium fetch failed for %s: %s", ",".join(c.trading_symbol for c in contracts), e)

    def _spot(self, sig: Signal) -> Optional[float]:
        u = self.unds.get(sig.underlying)
        if not u:
            return None
        if self.ws and self.ws.enabled and not self.ws.is_stale():
            t = self.ws.get(u.spot_key)
            if t and time.time() - t.ts < cfg.WS_STALE_SEC:
                return t.ltp
        cached = self._spot_cache.get(u.spot_key)          # websocket down: poll spot over REST so the underlying stop still works
        if cached and time.time() - cached[0] < cfg.REST_PREMIUM_REFRESH_SEC:
            return cached[1]
        try:
            v = self.fo.call("spot_ltp", u)
            self._spot_cache[u.spot_key] = (time.time(), v)
            return v
        except Exception:
            return None

    # ---- core ----
    def step(self, t: datetime):
        items = list(self.active.items())
        if not items:
            return
        need = [c for _, (_, c) in items if self._ws_premium(c) is None
                and not (self._rest_cache.get(c.instrument_key) and time.time() - self._rest_cache[c.instrument_key][0] < cfg.REST_PREMIUM_REFRESH_SEC)]
        self._refresh_rest(need)
        for sid, (sig, c) in items:
            prem = self._premium(c)
            if prem is None:
                if past_exit_time(t):
                    last = self._last_prem.get(sig.id)       # never pretend a no-quote exit was flat: use the last real price
                    self._close(sig, last if last else sig.ltp, "TIME", "TIME_EXIT",
                                "No live quote at exit time; marked at last known premium" if last
                                else "No quote seen since entry; marked at entry")
                continue
            self.evaluate(sig, prem, self._spot(sig), t)

    def evaluate(self, sig: Signal, prem: float, spot: Optional[float], t: datetime):
        """Pure state machine (unit-tested): updates `sig` and sends alerts."""
        sig.prem_high = max(sig.prem_high, prem)
        self._last_prem[sig.id] = prem
        if past_exit_time(t):
            return self._close(sig, prem, "TIME", "TIME_EXIT", "Hard exit time reached")
        if spot is not None and ((sig.side == "CALL" and spot <= sig.und_sl) or (sig.side == "PUT" and spot >= sig.und_sl)):
            return self._close(sig, prem, "UND_SL", "LOSS" if prem < sig.ltp else "BREAKEVEN",
                               f"Underlying hit stop {sig.und_sl:.2f} (spot {spot:.2f})")
        if prem <= sig.cur_sl:
            res = "LOSS" if sig.cur_sl < sig.ltp else ("WIN" if sig.cur_sl > sig.ltp else "BREAKEVEN")
            return self._close(sig, prem, "SL", res, "Stop-loss hit" if res == "LOSS" else "Trailing stop hit")
        # V3 tracks all three published milestones in order. T1 moves risk to
        # cost, T2 locks T1, and T3 is the final modeled exit. Processing one
        # milestone per quote also preserves the audit trail if a quote gaps
        # through multiple targets between tracker polls.
        if (sig.status == "ACTIVE" and prem >= sig.t1
                and (cfg.V3_PROFILE_ENABLED or prem < sig.t2)):
            sig.status = "T1_HIT"
            sig.cur_sl = max(sig.cur_sl, sig.ltp)
            self._notify(sig, "T1", prem, "Target 1 reached; protective SL moved to cost")
        elif cfg.V3_PROFILE_ENABLED and sig.status == "T1_HIT" and sig.t2 > 0 and prem >= sig.t2:
            sig.status = "T2_HIT"
            sig.cur_sl = max(sig.cur_sl, sig.t1)
            self._notify(sig, "T2", prem, f"Target 2 reached; protective SL locked at Target 1 (₹{sig.t1:.2f}); Target 3 remains active")
        elif cfg.V3_PROFILE_ENABLED and sig.status == "T2_HIT" and sig.t3 > 0 and prem >= sig.t3:
            return self._close(sig, prem, "T3", "WIN", "Target 3 reached; final modeled exit")
        elif not cfg.V3_PROFILE_ENABLED and prem >= sig.t2:
            return self._close(sig, prem, "T2", "WIN", "Target 2 reached")
        elif sig.status in {"T1_HIT", "T2_HIT"} and prem_high_trail(sig) and sig.cur_sl < sig.t1:
            sig.cur_sl = sig.t1
            self._notify(sig, "TRAIL", prem, f"SL trailed to Target 1 (₹{sig.t1:.2f})")
        else:
            self.db.save_signal(sig)

    def _notify(self, sig, kind, prem, note=""):
        self.db.save_signal(sig)
        self.db.add_update(sig.id, kind, prem, note)
        self.tg.send(format_update(sig, kind, prem, note))

    def _close(self, sig: Signal, prem: float, kind: str, result: str, note: str):
        # Entries are priced at the ask; the exit is a sell, so it fills on the bid side, and costs are deducted.
        # Otherwise tracked outcomes (and everything the adaptive layer learns from them) are flattering.
        fill = exit_fill_price(prem, sig.spread_pct) if cfg.REALISTIC_EXITS else prem
        sig.status, sig.exit_price, sig.exit_reason, sig.result = "CLOSED", fill, kind, result
        gross = (fill - sig.ltp) * sig.lot_size
        sig.pnl_lot = gross - (round_trip_cost(sig.ltp, fill, sig.lot_size) if cfg.REALISTIC_EXITS else 0.0)
        sig.modeled_pnl = sig.pnl_lot
        self._last_prem.pop(sig.id, None)
        with self._lock:
            self.active.pop(sig.id, None)
        self._notify(sig, kind if kind != "SL" else "SL", prem, note)
        if self.ws and self.ws.enabled:
            c_key = sig.instrument_key
            if not any(s.instrument_key == c_key for s, _ in self.active.values()):
                self.ws.unsubscribe([c_key])


def prem_high_trail(sig: Signal) -> bool:
    return sig.prem_high >= sig.t1 + 0.5 * (sig.t2 - sig.t1)
