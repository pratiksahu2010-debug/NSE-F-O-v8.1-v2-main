"""Market regime classification for 5-minute NSE signals.

Uses only finalized indicator values supplied by the snapshot builder.
Regime is context/ranking information; it does not blindly disable strategies.
"""
from __future__ import annotations


def classify_regime(indicators: dict) -> str:
    adx = indicators.get("adx_14")
    ema9, ema21, ema50 = (indicators.get(k) for k in ("ema_9", "ema_21", "ema_50"))
    atr_pct = indicators.get("atr_pct")
    bb_width = indicators.get("bb_width_pct")
    adx_slope = indicators.get("adx_slope_3")
    if any(v is None for v in (adx, ema9, ema21, ema50)):
        return "UNKNOWN"
    aligned_up = ema9 > ema21 > ema50
    aligned_down = ema9 < ema21 < ema50
    if adx >= 22 and (aligned_up or aligned_down):
        return "TRENDING_UP" if aligned_up else "TRENDING_DOWN"
    if (adx is not None and adx < 18 and bb_width is not None
            and bb_width <= 1.0):
        return "COMPRESSION"
    if ((atr_pct is not None and atr_pct >= 0.45)
            or (adx_slope is not None and adx_slope >= 2.0)):
        return "VOLATILITY_EXPANSION"
    if adx < 18:
        return "SIDEWAYS"
    return "CHOPPY"
