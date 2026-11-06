"""Dedicated Opening Range Breakout engine.

The closed-candle scorer and persisted optional retest lifecycle operate on
completed 5-minute candles. The separate live partial-candle ORB trigger is
implemented in engine.live_intrabar. Neither path places orders.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from statistics import median

from config import cfg
from indicators.core import adx, atr, close_pos, ema, rsi, vwap
from strategies.orb_universe import ORB_SYMBOL_SET

log = logging.getLogger("momentum_orb")
KV_KEY = "momentum_orb_lifecycle_v1"


def score_momentum_orb(session, allc):
    """Simple first-break ORB: 09:15-09:30 range, then clean acceptance outside it.

    A retest can be handled by the scanner lifecycle, but is not compulsory.
    Trend indicators are deliberately not stacked on top of the price structure.
    """
    if not cfg.MOMENTUM_ORB_ENABLED:
        return 0, 0.0, "ORB disabled", {}
    if len(session) < cfg.ORB_BARS + 1 or len(allc) < 30:
        return 0, 0.0, "ORB warmup", {}
    opening = [bar for bar in session if (bar.ts.hour, bar.ts.minute) >= (9, 15)
               and (bar.ts.hour, bar.ts.minute) < (9, 30)]
    if len(opening) != cfg.ORB_BARS or (opening[0].ts.hour, opening[0].ts.minute) != (9, 15):
        return 0, 0.0, "opening range must contain 09:15, 09:20 and 09:25 candles", {}
    for left, right in zip(opening, opening[1:]):
        if (right.ts - left.ts).total_seconds() != 300:
            return 0, 0.0, "opening range candles are not contiguous", {}
    last = session[-1]
    if (last.ts.hour, last.ts.minute) < (9, 30) or (last.ts.hour, last.ts.minute) > cfg.ORB_LAST_TIME:
        return 0, 0.0, "outside ORB entry window", {}
    hi, lo = max(x.h for x in opening), min(x.l for x in opening)
    ranges = [max(0.0, x.h - x.l) for x in allc[-20:]]
    base_range = sum(ranges[:-1]) / max(1, len(ranges) - 1)
    current_range = max(0.0, last.h - last.l)
    av = atr(allc, 14)[-1]
    if not av or av <= 0 or base_range <= 0:
        return 0, 0.0, "ORB volatility baseline unavailable", {}
    width = hi - lo
    if not (cfg.ORB_MIN_RANGE_ATR * av <= width <= cfg.ORB_MAX_RANGE_ATR * av):
        return 0, 0.0, "opening range outside ATR bounds", {}
    cp = close_pos(last)
    body = abs(last.c - last.o) / max(current_range, 1e-12)
    closes_after_range = [x for x in session if x.ts > opening[-1].ts and x.ts < last.ts]
    previous_close = closes_after_range[-1].c if closes_after_range else opening[-1].c
    closes = [x.c for x in allc]
    e9 = ema(closes, 9)[-1]
    for side in (1, -1):
        edge = hi if side > 0 else lo
        # A failed early break must not permanently consume the ORB. A fresh
        # reclaim/re-break is valid after price has returned inside the range.
        fresh = side * (previous_close - edge) <= 0
        clearance = side * (last.c - edge) / av
        accepted = clearance >= float(getattr(cfg, "MEX_ORB_CLEARANCE_ATR", 0.05))
        candle_direction = side * (last.c - last.o) > 0
        close_threshold = float(getattr(cfg, "MEX_ORB_CLOSE_POS", 0.65))
        close_strength = cp >= close_threshold if side > 0 else cp <= (1.0 - close_threshold)
        extension = side * (last.c - e9) / av
        not_chasing = extension <= cfg.MEX_ORB_MAX_EXTENSION_ATR
        if (fresh and accepted and candle_direction and close_strength
                and body >= float(getattr(cfg, "MEX_ORB_BODY_MIN", 0.50)) and not_chasing):
            metrics = {"orb_high": hi, "orb_low": lo, "orb_midpoint": (hi + lo) / 2.0,
                       "orb_width": width,
                       "atr": av, "breakout_clearance_atr": clearance,
                       "range_expansion": current_range / max(base_range, 1e-12),
                       "ema9": e9, "extension_atr": extension,
                       "relative_volume": (last.v / (sum(x.v for x in allc[-21:-1] if x.v > 0) / max(1, sum(1 for x in allc[-21:-1] if x.v > 0)))
                                            if last.v > 0 and any(x.v > 0 for x in allc[-21:-1]) else None),
                       "close_position": cp, "body_fraction": body}
            return side, 85.0, (
                f"ORB {'LONG' if side > 0 else 'SHORT'} breakout accepted beyond "
                f"{edge:.2f}; retest preferred but not mandatory"
            ), metrics
    return 0, 0.0, "no fresh accepted ORB breakout", {}


class MomentumORBScanner:
    """One-bar candidate -> next-bar retest/hold confirmation, persisted per symbol."""

    def __init__(self, db=None):
        self.db = db
        self.universe = ORB_SYMBOL_SET
        self.states = {}
        self.stats = {"eligible": 0, "candidates": 0, "confirmed": 0, "expired": 0, "invalidated": 0,
                      "no_trigger": 0, "rejection_reasons": {}}
        try:
            raw = db.kv_get(KV_KEY) if db is not None else None
            if raw:
                value = json.loads(raw)
                if isinstance(value, dict):
                    self.states = value
        except Exception:
            log.exception("Momentum+ORB lifecycle restore failed")

    def begin_cycle(self):
        self.stats = {"eligible": 0, "candidates": 0, "confirmed": 0, "expired": 0, "invalidated": 0,
                      "no_trigger": 0, "rejection_reasons": {}}

    def _persist(self):
        if self.db is not None:
            try:
                self.db.kv_set(KV_KEY, json.dumps(self.states))
            except Exception:
                log.exception("Momentum+ORB lifecycle persist failed")

    def scan(self, symbol, session, allc):
        empty = {"eligible": False, "confirmed_now": False, "side": 0, "score": 0.0,
                 "reason": "", "state": "IDLE", "metrics": {}}
        if symbol not in ORB_SYMBOL_SET or not cfg.MOMENTUM_ORB_ENABLED or not allc or not session:
            return empty
        self.stats["eligible"] += 1
        bar = allc[-1]
        cur = self.states.get(symbol, {})
        state = cur.get("state", "IDLE")
        if state == "CANDIDATE":
            if cur.get("bar") == bar.ts.isoformat():
                self.stats["candidates"] += 1
                return {**empty, "eligible": True, "side": cur["side"], "score": cur["score"],
                        "reason": cur["reason"], "state": "CANDIDATE", "metrics": cur.get("metrics", {})}
            # Candidates expire unless the immediately following completed 5m
            # candle arrives on the same trading day (no gap/session carry-over).
            try:
                candidate_bar = datetime.fromisoformat(str(cur["bar"]))
                consecutive_next_bar = (
                    candidate_bar.date() == bar.ts.date()
                    and (bar.ts - candidate_bar).total_seconds() == 300
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                consecutive_next_bar = False
            if not consecutive_next_bar:
                cur.update({
                    "state": "EXPIRED",
                    "last_bar": bar.ts.isoformat(),
                    "expiry_reason": "candidate was not followed by the immediately next 5-minute candle",
                })
                self.states[symbol] = cur
                self._persist()
                self.stats["expired"] += 1
                return {**empty, "eligible": True, "state": "EXPIRED",
                        "reason": cur["expiry_reason"], "metrics": cur.get("metrics", {})}

            side, edge, a = cur["side"], cur["edge"], float(cur["atr"])
            if side > 0:
                held = bar.c > edge and bar.l >= edge - cfg.MEX_ORB_RETEST_ATR * a
                retested = (bar.l <= edge + cfg.MEX_ORB_RETEST_ATR * a) if cfg.MEX_ORB_RETEST_REQUIRED else True
                candle_ok = bar.c > bar.o and close_pos(bar) >= 0.60
                invalid = bar.c <= edge - cfg.MEX_ORB_RETEST_ATR * a
            else:
                held = bar.c < edge and bar.h <= edge + cfg.MEX_ORB_RETEST_ATR * a
                retested = (bar.h >= edge - cfg.MEX_ORB_RETEST_ATR * a) if cfg.MEX_ORB_RETEST_REQUIRED else True
                candle_ok = bar.c < bar.o and close_pos(bar) <= 0.40
                invalid = bar.c >= edge + cfg.MEX_ORB_RETEST_ATR * a
            if held and retested and candle_ok:
                cur.update({"state": "CONFIRMED", "confirmed_bar": bar.ts.isoformat()})
                self.states[symbol] = cur
                self._persist()
                self.stats["confirmed"] += 1
                return {"eligible": True, "confirmed_now": True, "side": side, "score": cur["score"],
                        "reason": cur["reason"] + (" | next-candle ORB retest held and confirmed" if cfg.MEX_ORB_RETEST_REQUIRED else " | continuation held; retest optional"),
                        "state": "CONFIRMED", "metrics": cur.get("metrics", {}),
                        "setup_bar": cur.get("bar"), "confirmed_bar": cur.get("confirmed_bar"),
                        "confirmation_price": float(bar.c)}
            cur["state"] = "INVALIDATED" if invalid else "EXPIRED"
            cur["last_bar"] = bar.ts.isoformat()
            self.states[symbol] = cur
            self._persist()
            self.stats["invalidated" if invalid else "expired"] += 1
            return {**empty, "eligible": True, "state": cur["state"], "reason": "ORB retest failed"}
        if state == "CONFIRMED":
            if cur.get("confirmed_bar") == bar.ts.isoformat():
                return {**empty, "eligible": True, "state": "CONFIRMED", "reason": cur.get("reason", "")}
            self.states[symbol] = {"state": "CONSUMED", "last_bar": bar.ts.isoformat()}
            self._persist()
            return {**empty, "eligible": True, "state": "CONSUMED"}
        side, score, reason, metrics = score_momentum_orb(session, allc)
        if side and score >= cfg.MEX_ORB_SCORE_MIN:
            edge = metrics["orb_high"] if side > 0 else metrics["orb_low"]
            if not cfg.MEX_ORB_RETEST_REQUIRED:
                accepted_reason = reason + " | same-candle breakout accepted; retest optional"
                self.states[symbol] = {"state": "CONFIRMED", "side": side, "score": score,
                    "reason": accepted_reason, "metrics": metrics, "edge": edge, "atr": metrics["atr"],
                    "bar": bar.ts.isoformat(), "setup_bar": bar.ts.isoformat(),
                    "confirmed_bar": bar.ts.isoformat()}
                self._persist()
                self.stats["confirmed"] += 1
                return {"eligible": True, "confirmed_now": True, "side": side, "score": score,
                        "reason": accepted_reason, "state": "CONFIRMED", "metrics": metrics,
                        "setup_bar": bar.ts.isoformat(), "confirmed_bar": bar.ts.isoformat(),
                        "confirmation_price": float(bar.c)}
            self.states[symbol] = {"state": "CANDIDATE", "side": side, "score": score,
                "reason": reason, "metrics": metrics, "edge": edge, "atr": metrics["atr"],
                "bar": bar.ts.isoformat()}
            self._persist()
            self.stats["candidates"] += 1
            return {"eligible": True, "confirmed_now": False, "side": side, "score": score,
                    "reason": reason, "state": "CANDIDATE", "metrics": metrics}
        self.stats["no_trigger"] += 1
        reasons = self.stats["rejection_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1
        return {**empty, "eligible": True, "reason": reason, "state": "IDLE", "metrics": metrics}

    def snapshot(self):
        state_counts = {}
        for value in self.states.values():
            state = str(value.get("state", "IDLE"))
            state_counts[state] = state_counts.get(state, 0) + 1
        return {**self.stats, "rejection_reasons": dict(self.stats["rejection_reasons"]),
                "universe_size": len(ORB_SYMBOL_SET), "persisted_states": state_counts}


__all__ = ["MomentumORBScanner", "score_momentum_orb"]
