"""Per-strategy performance records and cache-age helpers (audit section B7 and B2)."""
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, List


def strategy_performance(signals: Iterable) -> dict:
    """Closed-signal outcomes per strategy. Open signals are counted separately and never as wins/losses."""
    agg = defaultdict(lambda: {"signals": 0, "open": 0, "wins": 0, "losses": 0, "breakeven": 0,
                               "time_exit": 0, "pnl_lot": 0.0})
    for s in signals:
        row = agg[getattr(s, "strategy", "") or "TREND"]
        row["signals"] += 1
        result = (getattr(s, "result", "") or "").upper()
        if not result:
            row["open"] += 1
            continue
        row["pnl_lot"] += float(getattr(s, "pnl_lot", 0.0) or 0.0)
        if result == "WIN":
            row["wins"] += 1
        elif result == "LOSS":
            row["losses"] += 1
        elif result == "BREAKEVEN":
            row["breakeven"] += 1
        elif result == "TIME_EXIT":
            row["time_exit"] += 1
    out = {}
    for name, row in agg.items():
        decided = row["wins"] + row["losses"]
        out[name] = dict(row, pnl_lot=round(row["pnl_lot"], 2),
                         win_rate=round(row["wins"] / decided, 3) if decided else None)
    return out


def rerank_due(cached_epoch: float, now_epoch: float, hours: float) -> bool:
    """True when a cached universe ranking is older than `hours` and should be recomputed."""
    if not cached_epoch:
        return True
    return (now_epoch - float(cached_epoch)) > hours * 3600.0


def strategy_floor(strategy: str, default: float, spec: str) -> float:
    """Per-strategy alert floor. `spec` looks like "REVERSAL=7.5,ORB=8". A strategy can only be made stricter:
    the result is never below `default` (the global ALERT_SCORE_MIN safety floor)."""
    overrides = {}
    for part in (spec or "").split(","):
        if "=" in part:
            name, value = part.split("=", 1)
            try:
                overrides[name.strip().upper()] = float(value)
            except ValueError:
                continue
    return max(float(default), overrides.get((strategy or "").upper(), float(default)))
