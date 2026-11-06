"""Dedicated fixed-universe Momentum Explosion scanner.

A valid MEI trigger is actionable on its detection candle; no dormant next-bar
lifecycle or persisted candidate state is involved.
"""
from __future__ import annotations

from config import cfg
from strategies.mex_universe import MEX_SYMBOLS
from strategies.momentum_explosion import momentum_expansion_index


class MomentumExplosionScanner:
    def __init__(self, db=None):
        self.universe = frozenset(MEX_SYMBOLS)
        self.stats = {"eligible": 0, "detected": 0, "pending": 0, "confirmed": 0,
                      "invalidated": 0, "expired": 0, "no_trigger": 0,
                      "rejection_reasons": {}, "gate_failures": {}, "disabled": not cfg.MEX_ENABLED}

    def begin_cycle(self):
        self.stats = {"eligible": 0, "detected": 0, "pending": 0, "confirmed": 0,
                      "invalidated": 0, "expired": 0, "no_trigger": 0,
                      "rejection_reasons": {}, "disabled": not cfg.MEX_ENABLED}

    def scan(self, symbol: str, session, all_candles) -> dict:
        empty = {"eligible": False, "sgn": None, "score": None, "reason": "",
                 "state": "IDLE", "live": {}, "metrics": {}}
        if not cfg.MEX_ENABLED or symbol not in self.universe or not all_candles:
            return empty
        self.stats["eligible"] += 1
        candidates = []
        rejected_directions = {}
        for sign in (1, -1):
            score, detector_reason, detector_metrics = momentum_expansion_index(sign, session, all_candles)
            if score > 0:
                candidates.append((score, sign, detector_reason, detector_metrics))
            else:
                rejected_directions["CALL" if sign > 0 else "PUT"] = {
                    "reason": detector_reason,
                    "gate_failures": list(detector_metrics.get("gate_failures", [])),
                    "gate_results": dict(detector_metrics.get("gate_results", {})),
                    "metrics": detector_metrics,
                }
        candidate = max(candidates, key=lambda row: row[0]) if candidates else None
        sign = candidate[1] if candidate else None
        score = candidate[0] if candidate else None
        reason = candidate[2] if candidate else "no early momentum expansion"
        metrics = candidate[3] if candidate else {"rejected_directions": rejected_directions}
        if candidate:
            self.stats["detected"] += 1
        else:
            self.stats["no_trigger"] += 1
            reason_counts = self.stats["rejection_reasons"]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            gate_counts = self.stats.setdefault("gate_failures", {})
            for direction in rejected_directions.values():
                for gate in direction.get("gate_failures", []):
                    gate_counts[gate] = gate_counts.get(gate, 0) + 1
        state = "CONFIRMED" if candidate else "IDLE"
        if candidate:
            self.stats["confirmed"] += 1
        return {"eligible": True, "sgn": sign, "score": score, "reason": reason,
                "state": state,
                "live": ({"state": state, "sgn": sign, "score": score, "reason": reason}
                         if candidate else {"state": "IDLE", "sgn": 0}),
                "metrics": metrics}

    def snapshot(self) -> dict:
        return {**self.stats, "rejection_reasons": dict(self.stats["rejection_reasons"]),
                "universe_size": len(self.universe)}
