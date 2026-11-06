"""Market regime: gap (decaying), India VIX state/direction, chop vs trend (ADX), expiry-day flag."""
from datetime import date
from typing import List, Optional

from config import cfg
from indicators.core import adx
from models import Candle


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def regime(session: List[Candle], prev_close: Optional[float], vix_session: List[Candle],
           allc: List[Candle], today: date, is_expiry_day: bool) -> dict:
    notes = []
    gap_score = 0.0
    if session and prev_close:
        gap = (session[0].o - prev_close) / prev_close * 100
        last = session[-1]
        mins = (last.ts.hour * 60 + last.ts.minute) - (9 * 60 + 15)
        decay = max(0.0, 1.0 - mins / cfg.GAP_DECAY_MIN)             # a 9:15 gap says little about 1 pm
        alive = (gap > 0 and last.c > prev_close) or (gap < 0 and last.c < prev_close)   # gap filled = gap thesis dead
        if alive:
            gap_score = _clamp(gap / 1.0) * 0.5 * decay
        if abs(gap) >= 0.4:
            notes.append(f"Gap {gap:+.2f}%" + ("" if alive else " (filled)"))
    vix_score, vix_level = 0.0, None
    if len(vix_session) >= 2:
        vix_level = vix_session[-1].c
        chg = (vix_session[-1].c - vix_session[0].o) / vix_session[0].o * 100 if vix_session[0].o else 0
        if chg > 2:
            vix_score = -0.3
            notes.append(f"VIX rising {chg:+.1f}%")
        elif chg < -2:
            vix_score = 0.3
            notes.append(f"VIX falling {chg:+.1f}%")
    warm = len(allc) >= 29                                   # ADX needs 2n+1 candles of warm-up
    adx_v = adx(allc, 14)[-1][0] if warm else 0.0
    chop = warm and adx_v < cfg.ADX_RISING_FLOOR              # ADX below the trend floor = range-bound
    if chop:
        notes.append(f"Choppy (ADX {adx_v:.0f})")
    if warm and adx_v:
        notes.append(f"Trend strength ADX {adx_v:.0f}")
    if is_expiry_day:
        notes.append("Expiry day")
    if is_expiry_day:
        label = "EXPIRY"
    elif vix_level and vix_level > 22:
        label = "VOLATILE"
    elif chop:
        label = "SIDEWAYS"
    elif warm and adx_v >= cfg.ADX_MIN:
        label = "TRENDING"
    else:
        label = "MIXED"
    return {
        "label": label,
        "score": _clamp(gap_score + vix_score),
        "chop": chop,
        "adx": adx_v,
        "vix": vix_level,
        "high_vix": bool(vix_level and vix_level > 22),
        "expiry_day": is_expiry_day,
        "notes": notes,
    }
