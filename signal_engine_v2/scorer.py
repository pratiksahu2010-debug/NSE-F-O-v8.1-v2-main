"""Unified 100-point candidate scorer.

Strategy modules detect a pattern; this scorer independently evaluates the
quality of that pattern and its market context. Missing data is neutral, not
silently treated as a bullish or bearish confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .models import Candidate, MarketSnapshot, Side


@dataclass(frozen=True)
class ScoreBreakdown:
    setup_quality: float
    trend_alignment: float
    vwap_context: float
    adx_direction: float
    relative_volume: float
    momentum: float
    higher_timeframe: float
    candle_quality: float
    volatility_quality: float

    @property
    def total(self) -> float:
        return round(sum((
            self.setup_quality, self.trend_alignment, self.vwap_context,
            self.adx_direction, self.relative_volume, self.momentum,
            self.higher_timeframe, self.candle_quality, self.volatility_quality
        )), 2)

    def as_dict(self):
        return {
            "setup_quality": round(self.setup_quality, 2),
            "trend_alignment": round(self.trend_alignment, 2),
            "vwap_context": round(self.vwap_context, 2),
            "adx_direction": round(self.adx_direction, 2),
            "relative_volume": round(self.relative_volume, 2),
            "momentum": round(self.momentum, 2),
            "higher_timeframe": round(self.higher_timeframe, 2),
            "candle_quality": round(self.candle_quality, 2),
            "volatility_quality": round(self.volatility_quality, 2),
            "total": self.total,
        }


def _v(snapshot, key):
    x = snapshot.indicators.get(key)
    return float(x) if isinstance(x, (int, float)) else None


def score_candidate(candidate: Candidate, snapshot: MarketSnapshot):
    side = candidate.side
    bull = side is Side.CALL
    wanted_direction = 1 if bull else -1
    c = snapshot.candle

    # Pattern-specific detector has already enforced the setup trigger.
    setup = max(0.0, min(20.0, (candidate.score - 60.0) * 0.8))

    e9, e21, e50 = _v(snapshot, "ema_9"), _v(snapshot, "ema_21"), _v(snapshot, "ema_50")
    if None in (e9, e21, e50):
        trend = 7.5
    else:
        aligned = (e9 > e21 > e50) if bull else (e9 < e21 < e50)
        partial = (e9 > e21) if bull else (e9 < e21)
        trend = 15.0 if aligned else (9.0 if partial else 0.0)

    vwap = _v(snapshot, "session_vwap")
    slope = _v(snapshot, "vwap_slope_3")
    if vwap is None:
        vwap_score = 5.0
    else:
        on_side = c.close > vwap if bull else c.close < vwap
        slope_ok = slope is None or (slope > 0 if bull else slope < 0)
        vwap_score = 10.0 if on_side and slope_ok else (5.0 if on_side else 0.0)

    adxv, pdi, ndi = _v(snapshot, "adx_14"), _v(snapshot, "plus_di"), _v(snapshot, "minus_di")
    if adxv is None or pdi is None or ndi is None:
        adx_score = 0.0
    else:
        directional = pdi > ndi if bull else ndi > pdi
        adx_score = 10.0 if adxv >= 22 and directional else 0.0

    rvol = _v(snapshot, "rvol_20")
    if rvol is None:
        volume_score = 7.5
    else:
        volume_score = max(0.0, min(15.0, (rvol - 0.7) * 12.5))

    rsi, hist = _v(snapshot, "rsi_14"), _v(snapshot, "macd_hist")
    if rsi is None and hist is None:
        momentum = 5.0
    else:
        points = 0.0
        if rsi is not None and ((50 <= rsi <= 78) if bull else (22 <= rsi <= 50)):
            points += 5.0
        if hist is not None and ((hist > 0) if bull else (hist < 0)):
            points += 5.0
        momentum = points

    # Relative strength confirms leadership but is not a mandatory gate.
    rs = _v(snapshot, "relative_strength_vs_index")
    rs_bonus = 5.0 if rs is not None and ((rs > 0) if bull else (rs < 0)) else 0.0

    # Expansion can be large in either direction. Only reward it when its
    # directional votes agree with this candidate.
    impulse_direction = snapshot.metadata.get("impulse_direction", 0)
    try:
        impulse_direction = int(impulse_direction)
    except (TypeError, ValueError):
        impulse_direction = 0
    directional_impulse = snapshot.metadata.get("directional_impulse_score", 0.0)
    try:
        directional_impulse = float(directional_impulse)
    except (TypeError, ValueError):
        directional_impulse = 0.0
    impulse_bonus = (
        max(0.0, min(5.0, directional_impulse))
        if impulse_direction == wanted_direction else 0.0
    )

    regime = str(snapshot.metadata.get("market_regime", "UNKNOWN")).upper()
    trend_regime = "TRENDING_UP" if bull else "TRENDING_DOWN"
    regime_bonus = 2.0 if regime in {trend_regime, "VOLATILITY_EXPANSION"} else 0.0
    htf = snapshot.metadata.get("htf_bias")
    htf_score = 10.0 if htf == wanted_direction else (0.0 if htf == -wanted_direction else 5.0)

    width = c.high - c.low
    body = abs(c.close - c.open) / width if width > 0 else 0.0
    close_pos = (c.close - c.low) / width if width > 0 else 0.5
    close_quality = close_pos if bull else 1.0 - close_pos
    candle_score = min(5.0, body * 2.5 + close_quality * 2.5)

    atr = _v(snapshot, "atr_14")
    atr_pct = (atr / c.close * 100.0) if atr and c.close > 0 else None
    if atr_pct is None:
        volatility = 2.5
    elif 0.08 <= atr_pct <= 2.5:
        volatility = 5.0
    elif 0.04 <= atr_pct < 0.08 or 2.5 < atr_pct <= 4.0:
        volatility = 2.5
    else:
        volatility = 0.0

    setup = max(0.0, min(20.0, setup + rs_bonus + impulse_bonus + regime_bonus))
    breakdown = ScoreBreakdown(setup, trend, vwap_score, adx_score, volume_score,
                               momentum, htf_score, candle_score, volatility)
    grade = "A+" if breakdown.total >= 85 else "A" if breakdown.total >= 75 else "B" if breakdown.total >= 65 else "REJECT"
    analytics = {
        "market_regime": regime,
        "move_potential": str(snapshot.metadata.get("move_potential", "NORMAL")),
        "impulse_score": _v(snapshot, "impulse_score") or 0.0,
        "impulse_direction": impulse_direction,
        "directional_impulse_score": directional_impulse,
        "impulse_tags": tuple(snapshot.metadata.get("impulse_tags", ())),
        "relative_strength_vs_index": rs,
    }
    enriched = replace(candidate, score=breakdown.total,
                       metadata={**dict(candidate.metadata), **analytics,
                                 "score_breakdown": breakdown.as_dict(),
                                 "strategy_raw_score": candidate.score, "grade": grade})
    return enriched, breakdown
