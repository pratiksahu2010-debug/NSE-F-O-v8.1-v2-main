"""Independent context evidence for a candidate signal.

EMA/VWAP/RSI/ADX are all functions of the same price series, so agreement between them is weaker confirmation than
it looks. This module adds information that is NOT derived from the signal candle's own indicators:

  * higher-timeframe (15m) trend and the NIFTY trend for stocks
  * relative strength of a stock vs NIFTY on the day
  * trigger-candle volume vs the SAME time of day on previous days (the 20-candle average mixes open and lunch)
  * dominant option-OI wall sitting right in the trade's path
  * index-futures OI change into the move (diagnostic only; no uncalibrated veto)

compute_features() is pure and returns a plain dict (also persisted per signal for calibration);
veto_from() turns that dict into an optional (code, detail) rejection.
"""
from typing import List, Optional, Tuple

from config import cfg
from models import Candle


def session_return_pct(session: List[Candle]) -> Optional[float]:
    if not session or session[0].o <= 0:
        return None
    return (session[-1].c / session[0].o - 1.0) * 100.0


def tod_vol_ratio(allc: List[Candle], min_days: Optional[int] = None) -> Optional[float]:
    """Last candle volume / mean volume of the same HH:MM candle on earlier days. None when unusable."""
    min_days = cfg.VOL_TOD_MIN_DAYS if min_days is None else min_days
    if not allc or allc[-1].v <= 0:
        return None
    last = allc[-1]
    hm = (last.ts.hour, last.ts.minute)
    same = [x.v for x in allc[:-1] if x.ts.date() < last.ts.date() and (x.ts.hour, x.ts.minute) == hm and x.v > 0]
    if len(same) < min_days:
        return None
    return last.v / (sum(same) / len(same))


def wall_info(chain, side: str, spot: float, atr_val: float) -> Optional[dict]:
    """Dominant OI wall in the direction of the trade (calls above spot for a CALL, puts below spot for a PUT)."""
    if chain is None or atr_val <= 0 or spot <= 0:
        return None
    ot = "CE" if side == "CALL" else "PE"
    w = chain.wall(ot, spot)
    if not w:
        return None
    strike, oi = w
    ois = [q.oi for (s, t), q in chain.rows.items()
           if t == ot and q.oi > 0 and ((s >= spot) if ot == "CE" else (s <= spot))]
    if len(ois) < 3:
        return None
    mean = sum(ois) / len(ois)
    dominant = mean > 0 and oi >= cfg.WALL_DOMINANCE * mean
    dist = (strike - spot) if ot == "CE" else (spot - strike)
    return {"strike": float(strike), "dist_atr": dist / atr_val, "dominant": bool(dominant)}


def fut_oi_change_pct(fut: Optional[List[Candle]], bars: Optional[int] = None) -> Optional[float]:
    bars = cfg.FUT_OI_BARS if bars is None else bars
    if not fut or len(fut) <= bars:
        return None
    a, b = fut[-1 - bars].oi, fut[-1].oi
    if a <= 0 or b <= 0:
        return None
    return (b / a - 1.0) * 100.0


def compute_features(kind: str, side: str, allc: List[Candle], session: List[Candle], chain, atr_val: float,
                     spot: float, htf: int, mkt_bias: int, mkt_ret: Optional[float],
                     fut: Optional[List[Candle]] = None) -> dict:
    sgn = 1 if side == "CALL" else -1
    own_ret = session_return_pct(session)
    rs = (own_ret - mkt_ret) if (kind == "STOCK" and own_ret is not None and mkt_ret is not None) else None
    wall = wall_info(chain, side, spot, atr_val)
    return {
        "side_sign": sgn, "htf": htf, "mkt_bias": mkt_bias if kind == "STOCK" else 0,
        "session_ret_pct": None if own_ret is None else round(own_ret, 3),
        "rs_pct": None if rs is None else round(rs, 3),
        "tod_vol_ratio": None if tod_vol_ratio(allc) is None else round(tod_vol_ratio(allc), 3),
        "wall_strike": wall["strike"] if wall else None,
        "wall_dist_atr": round(wall["dist_atr"], 3) if wall else None,
        "wall_dominant": wall["dominant"] if wall else None,
        "fut_oi_chg_pct": None if fut_oi_change_pct(fut) is None else round(fut_oi_change_pct(fut), 3),
    }


def veto_from(f: dict, kind: str, is_reversal: bool, ignore_trend_context: bool = False) -> Optional[Tuple[str, str]]:
    """First failing context gate as (short code, detail), else None. Reversals are counter-trend by design, so the
    trend-alignment gates (15m, NIFTY, relative strength) do not apply to them; participation and wall gates do."""
    sgn = f["side_sign"]
    if not is_reversal:
        if not ignore_trend_context and cfg.HTF_VETO_ENABLED and f["htf"] == -sgn:
            return "15m trend opposed", "15m EMA trend is against this side"
        if not ignore_trend_context and cfg.MKT_VETO_ENABLED and kind == "STOCK" and f["mkt_bias"] == -sgn:
            return "NIFTY trend opposed", "stock signal against the NIFTY 15m trend"
        rs = f.get("rs_pct")
        if cfg.RS_GATE_ENABLED and kind == "STOCK" and rs is not None and sgn * rs < -cfg.RS_TOLERANCE_PCT:
            return "weak relative strength", f"stock {rs:+.2f}% vs NIFTY on the day"
    tv = f.get("tod_vol_ratio")
    if cfg.VOL_TOD_GATE_ENABLED and tv is not None and tv < cfg.VOL_TOD_MIN:
        return "thin time-of-day volume", f"{tv:.2f}x same-time average"
    d = f.get("wall_dist_atr")
    if cfg.WALL_GATE_ENABLED and f.get("wall_dominant") and d is not None and 0 <= d <= cfg.WALL_MIN_ATR:
        return "into OI wall", f"dominant wall {f['wall_strike']:g} only {d:.2f} ATR away"
    return None
