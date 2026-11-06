"""Big-move acceleration metrics; a score, not a standalone entry trigger."""
from __future__ import annotations


def detect_impulse(indicators: dict, candle) -> dict:
    """Measure expansion separately from its direction.

    Expansion describes the chance of a large move. Directional votes decide
    whether that expansion supports CALL or PUT; a bearish impulse must never
    add confidence to a bullish candidate (or vice versa).
    """
    atr = indicators.get("atr_14")
    rvol = indicators.get("rvol_20")
    adx = indicators.get("adx_14")
    adx_slope = indicators.get("adx_slope_3")
    ema9, ema21 = indicators.get("ema_9"), indicators.get("ema_21")
    hist, prev_hist = indicators.get("macd_hist"), indicators.get("macd_hist_prev")
    pdi, ndi = indicators.get("plus_di"), indicators.get("minus_di")

    expansion_score = 0.0
    tags = []
    if atr is not None and atr > 0:
        expansion = (candle.high - candle.low) / atr
        if expansion >= 1.2:
            expansion_score += 1.0
            tags.append("range_expansion")
    if rvol is not None and rvol >= 1.5:
        expansion_score += 1.0
        tags.append("relative_volume_expansion")
    if adx is not None and adx >= 22 and adx_slope is not None and adx_slope > 0:
        expansion_score += 1.0
        tags.append("adx_acceleration")
    if ema9 is not None and ema21 is not None and atr is not None and atr > 0:
        if abs(ema9 - ema21) / atr >= 0.25:
            expansion_score += 1.0
            tags.append("ema_separation")
    if hist is not None and prev_hist is not None and abs(hist) > abs(prev_hist):
        expansion_score += 1.0
        tags.append("macd_histogram_expansion")

    bullish = 0
    bearish = 0
    directional_tags = []
    if ema9 is not None and ema21 is not None and ema9 != ema21:
        if ema9 > ema21:
            bullish += 1
            directional_tags.append("bullish_ema_alignment")
        else:
            bearish += 1
            directional_tags.append("bearish_ema_alignment")
    if hist is not None and hist != 0:
        if hist > 0:
            bullish += 1
            directional_tags.append("positive_macd_histogram")
        else:
            bearish += 1
            directional_tags.append("negative_macd_histogram")
    if hist is not None and prev_hist is not None and hist != prev_hist:
        if hist > prev_hist:
            bullish += 1
            directional_tags.append("macd_momentum_rising")
        else:
            bearish += 1
            directional_tags.append("macd_momentum_falling")
    if pdi is not None and ndi is not None and pdi != ndi:
        if pdi > ndi:
            bullish += 1
            directional_tags.append("positive_directional_di")
        else:
            bearish += 1
            directional_tags.append("negative_directional_di")
    if candle.close > candle.open:
        bullish += 1
        directional_tags.append("bullish_candle")
    elif candle.close < candle.open:
        bearish += 1
        directional_tags.append("bearish_candle")

    if bullish >= 3 and bullish > bearish:
        direction, directional_score = 1, float(min(5, bullish))
    elif bearish >= 3 and bearish > bullish:
        direction, directional_score = -1, float(min(5, bearish))
    else:
        direction, directional_score = 0, 0.0

    return {
        "impulse_score": expansion_score,
        "impulse_direction": direction,
        "directional_impulse_score": directional_score,
        "impulse_tags": tuple(tags + directional_tags),
        "move_potential": (
            "EXPLOSIVE" if expansion_score >= 4
            else "STRONG" if expansion_score >= 3
            else "NORMAL"
        ),
    }
