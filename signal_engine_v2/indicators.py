"""Shared, deterministic indicator snapshot builder.

Calculates each indicator once per market snapshot. Index volume is left unknown
unless a reliable volume-weighted proxy has been attached by the data layer.
"""
from __future__ import annotations

from math import sqrt
from types import SimpleNamespace
from typing import Dict, Sequence

from indicators.core import (adx, atr, body_frac, close_pos, ema, macd, rsi,
                             vwap)
from .market_regime import classify_regime
from .impulse_detector import detect_impulse
from .relative_strength import relative_strength, sector_relative_strength


def _legacy(candles):
    # Adapt the v2 immutable candle model to the existing tested pure-Python core.
    return [SimpleNamespace(ts=c.timestamp, o=c.open, h=c.high, l=c.low,
                            c=c.close, v=c.volume, oi=0.0) for c in candles]


def _strict_rvol_20(volumes: Sequence[float]) -> float | None:
    """RVOL is valid only with 20 complete positive-volume reference bars."""
    if len(volumes) < 21:
        return None
    prior = [float(value) for value in volumes[-21:-1]]
    current = float(volumes[-1])
    if current <= 0 or len(prior) != 20 or any(value <= 0 for value in prior):
        return None
    baseline = sum(prior) / 20.0
    return current / baseline if baseline > 0 else None


def calculate(candles: Sequence, session_candles: Sequence | None = None) -> Dict[str, float | None]:
    bars = list(candles)
    if not bars:
        return {}
    legacy = _legacy(bars)
    closes = [float(c.close) for c in bars]
    highs = [float(c.high) for c in bars]
    lows = [float(c.low) for c in bars]
    volumes = [float(c.volume) for c in bars]
    out: Dict[str, float | None] = {}

    for period in (9, 21, 50, 200):
        series = ema(closes, period)
        out[f"ema_{period}"] = series[-1] if len(series) >= period else None

    rs = rsi(closes, 14)
    out["rsi_14"] = rs[-1] if len(bars) >= 15 else None
    m_line, m_signal, m_hist = macd(closes, 12, 26, 9)
    out["macd"] = m_line[-1] if len(bars) >= 35 else None
    out["macd_signal"] = m_signal[-1] if len(bars) >= 35 else None
    out["macd_hist"] = m_hist[-1] if len(bars) >= 35 else None
    out["macd_hist_prev"] = m_hist[-2] if len(bars) >= 36 else None

    adx_series = adx(legacy, 14) if len(bars) >= 29 else []
    av = adx_series[-1] if adx_series else (None, None, None)
    out["adx_14"], out["plus_di"], out["minus_di"] = av
    out["adx_slope_3"] = (adx_series[-1][0] - adx_series[-4][0]) if len(adx_series) >= 4 and None not in (adx_series[-1][0], adx_series[-4][0]) else None
    out["atr_14"] = atr(legacy, 14)[-1] if len(bars) >= 14 else None
    out["atr_pct"] = (out["atr_14"] / closes[-1] * 100.0) if out["atr_14"] and closes[-1] > 0 else None

    session = list(session_candles) if session_candles is not None else bars
    session_legacy = _legacy(session)
    vw = vwap(session_legacy)
    out["session_vwap"] = vw[-1] if vw else None
    out["vwap_slope_3"] = (vw[-1] - vw[-4]) if len(vw) >= 4 else None
    out["rvol_20"] = _strict_rvol_20(volumes)

    last = legacy[-1]
    out["candle_body_fraction"] = body_frac(last)
    out["candle_close_position"] = close_pos(last)
    if len(bars) >= 21:
        out["donchian_high_20"] = max(highs[-21:-1])
        out["donchian_low_20"] = min(lows[-21:-1])
    else:
        out["donchian_high_20"] = out["donchian_low_20"] = None

    if len(closes) >= 20:
        mean = sum(closes[-20:]) / 20
        variance = sum((x - mean) ** 2 for x in closes[-20:]) / 20
        sd = sqrt(variance)
        upper, lower = mean + 2 * sd, mean - 2 * sd
        out["bb_mid_20"] = mean
        out["bb_upper_20_2"] = upper
        out["bb_lower_20_2"] = lower
        out["bb_width_pct"] = ((upper - lower) / mean * 100) if mean else None
    else:
        out["bb_mid_20"] = out["bb_upper_20_2"] = out["bb_lower_20_2"] = out["bb_width_pct"] = None

    # OBV is cumulative direction-weighted volume; None when volume is unavailable.
    if len(bars) >= 21 and all(v > 0 for v in volumes[-21:]):
        obv = 0.0
        for i in range(1, len(bars)):
            if closes[i] > closes[i - 1]:
                obv += volumes[i]
            elif closes[i] < closes[i - 1]:
                obv -= volumes[i]
        out["obv"] = obv
    else:
        out["obv"] = None

    # MFI requires real volume; return None rather than manufacturing a neutral value.
    if len(bars) >= 15 and all(v > 0 for v in volumes[-15:]):
        flows = []
        for i in range(1, len(bars)):
            tp = (highs[i] + lows[i] + closes[i]) / 3
            prev_tp = (highs[i - 1] + lows[i - 1] + closes[i - 1]) / 3
            flows.append((tp * volumes[i], 1 if tp > prev_tp else -1 if tp < prev_tp else 0))
        recent = flows[-14:]
        positive = sum(v for v, d in recent if d > 0)
        negative = sum(v for v, d in recent if d < 0)
        out["mfi_14"] = 100.0 if negative == 0 and positive > 0 else (50.0 if negative == positive == 0 else
                           100.0 - 100.0 / (1.0 + positive / negative))
    else:
        out["mfi_14"] = None

    return out


def enrich_snapshot(snapshot, session_candles=None):
    """Return a copy with indicators calculated from closed candles only.

    The current session is supplied separately so session VWAP resets at the
    NSE open. Intrabar price is not included in any indicator baseline.
    """
    from dataclasses import replace
    closed = [bar for bar in snapshot.candles if bar.is_closed]
    if snapshot.candle.is_closed and (not closed or closed[-1].timestamp != snapshot.candle.timestamp):
        closed.append(snapshot.candle)
    session = list(session_candles) if session_candles is not None else [
        bar for bar in closed if bar.timestamp.date() == snapshot.candle.timestamp.date()
    ]
    values = calculate(closed, session)
    # Index spot has no true traded volume. Never let the partial history
    # accidentally satisfy RVOL using only a handful of current-session bars.
    session_legacy = _legacy(session)
    if str(snapshot.instrument_kind).upper() == "INDEX":
        values["rvol_20"] = None
    if len(session_legacy) >= 21 and all(x.v > 0 for x in session_legacy[-21:]):
        values["rvol_20"] = _strict_rvol_20([x.v for x in session_legacy])
    meta = dict(snapshot.metadata)
    meta["relative_strength_vs_index"] = relative_strength(
        meta.get("underlying_return_pct"), meta.get("index_return_pct"))
    meta["relative_strength_vs_sector"] = sector_relative_strength(
        meta.get("underlying_return_pct"), meta.get("sector_return_pct"))
    values["relative_strength_vs_index"] = meta["relative_strength_vs_index"]
    meta["market_regime"] = classify_regime(values)
    impulse = detect_impulse(values, snapshot.candle)
    values["impulse_score"] = impulse["impulse_score"]
    meta.update(impulse)
    return replace(snapshot, indicators=values, metadata=meta)
