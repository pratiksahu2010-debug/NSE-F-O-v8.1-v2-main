"""Self-checking accuracy layer.

The bot tracks every signal to its outcome (SL / T1 / T2 / time exit). This module reads the last ADAPT_DAYS of closed
signals and decides how much extra confidence a *new* signal needs, per
  * signal type            (TREND / REVERSAL / ORB / VWAP / SQUEEZE / MOMENTUM_EXPLOSION)
  * type x kind            ("ORB|STOCK": index and stock behaviour differ, so they are judged separately)
  * time-of-day bucket     ("tod:11:30": learned from own outcomes instead of fixed lunch/late penalties)

Profit factor is computed on R-multiples (P&L / premium risked, net of modelled exit slippage and costs), so index
lots and stock lots are comparable and one big-lot trade cannot dominate.

  profit factor >= 1.5          proven      (reported; the bar is never LOWERED automatically)
  0.9 <= profit factor < 1.5    normal       0
  0.6 <= profit factor < 0.9    weak        +6
  profit factor < 0.6           probation  +12  (never fully muted, so the type keeps producing outcomes)

Nothing changes until ADAPT_MIN_N closed signals exist for that bucket. extra_score() is tighten-only: the live gate
(ALERT_SCORE_MIN, floor 8/10) can be raised by learning but never relaxed by it.
"""
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from config import cfg
from engine.outcomes import profit_factor, r_multiple


def _pf(pnls: List[float]) -> float:
    return profit_factor(pnls)


def delta_for(n: int, pf: float) -> Tuple[float, str]:
    if n < cfg.ADAPT_MIN_N:
        return 0.0, f"learning ({n}/{cfg.ADAPT_MIN_N} closed signals)"
    if pf >= 1.5:
        return -3.0, f"proven (PF {pf:.2f}, n={n})"
    if pf >= 0.9:
        return 0.0, f"normal (PF {pf:.2f}, n={n})"
    if pf >= 0.6:
        return 6.0, f"weak (PF {pf:.2f}, n={n}): +6 bar"
    return 12.0, f"probation (PF {pf:.2f}, n={n}): +12 bar"


def tod_bucket(ts) -> str:
    """'tod:HH:MM' bucket start for an ISO timestamp / datetime."""
    try:
        t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return ""
    step = max(5, int(cfg.ADAPT_TOD_MINUTES))
    return f"tod:{t.hour:02d}:{(t.minute // step) * step:02d}"


class Adaptive:
    def __init__(self, db):
        self.db = db
        self._at = 0.0
        self._cache: Dict[str, Tuple[float, str]] = {}

    def _refresh(self, now):
        if time.monotonic() - self._at < 300 and self._cache:
            return
        start = (now.date() - timedelta(days=cfg.ADAPT_DAYS)).isoformat()
        by: Dict[str, List[float]] = {}
        for s in self.db.closed_since(start):
            r = r_multiple(s)
            strat = s.strategy or "TREND"
            by.setdefault(strat, []).append(r)
            if getattr(s, "kind", ""):
                by.setdefault(f"{strat}|{s.kind}", []).append(r)
            b = tod_bucket(s.ts)
            if b:
                by.setdefault(b, []).append(r)
            if s.tier == "EARLY":
                by.setdefault("tier:EARLY", []).append(r)
        self._cache = {k: delta_for(len(v), _pf(v)) for k, v in by.items()}
        self._at = time.monotonic()

    def delta(self, key: str, now) -> Tuple[float, str]:
        """(extra confidence points required on the 0-100 scale, human note) for a bucket key."""
        if not cfg.ADAPT_ENABLED:
            return 0.0, ""
        self._refresh(now)
        return self._cache.get(key, (0.0, ""))

    def extra_score(self, strategy: str, kind: str, now) -> Tuple[float, str]:
        """Extra points on the 0-10 alert scale a new signal needs. Tighten-only (never negative).
        Uses the type|kind bucket once it has enough data, else the type bucket; adds a capped time-of-day term."""
        if not cfg.ADAPT_ENABLED:
            return 0.0, ""
        notes = []
        pts = 0.0
        for key in (f"{strategy}|{kind}" if kind else "", strategy):
            if not key:
                continue
            d, note = self.delta(key, now)
            if note and not note.startswith("learning"):
                pts += max(0.0, d)
                notes.append(f"{key}: {note}")
                break
        tb = tod_bucket(now)
        if tb:
            d, note = self.delta(tb, now)
            if note and not note.startswith("learning") and d > 0:
                pts += min(d, cfg.ADAPT_TOD_CAP_PTS)
                notes.append(f"{tb}: {note}")
        return pts / 10.0, "; ".join(notes)

    def snapshot(self, now) -> Dict[str, str]:
        self._refresh(now)
        return {k: v[1] for k, v in self._cache.items()}
