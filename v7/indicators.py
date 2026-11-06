"""Pine-style EMA, Wilder RSI/ATR/DMI-ADX and session VWAP helpers."""
from __future__ import annotations

from typing import Sequence
from models import Candle


def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = []
    current = None
    for value in values:
        value = float(value)
        current = value if current is None else alpha * value + (1.0 - alpha) * current
        out.append(current)
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out = []
    running = 0.0
    for i, value in enumerate(values):
        running += float(value)
        if i >= period:
            running -= float(values[i - period])
        out.append(running / period if i >= period - 1 else None)
    return out


def wilder(values: Sequence[float], period: int) -> list[float | None]:
    """Wilder RMA: SMA seed over the first period observations, then recursive smoothing."""
    if period <= 0:
        raise ValueError("period must be positive")
    out = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(float(x) for x in values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = (current * (period - 1) + float(values[i])) / period
        out[i] = current
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """ta.atr-style Wilder ATR; includes inter-session gaps via previous close."""
    if not candles:
        return []
    true_ranges = []
    for i, candle in enumerate(candles):
        if i == 0:
            true_ranges.append(float(candle.h - candle.l))
        else:
            prev_close = float(candles[i - 1].c)
            true_ranges.append(max(
                float(candle.h - candle.l),
                abs(float(candle.h) - prev_close),
                abs(float(candle.l) - prev_close),
            ))
    return wilder(true_ranges, period)


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """ta.rsi-style Wilder RSI, seeded from the first period close-to-close changes."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains = [max(float(closes[i]) - float(closes[i - 1]), 0.0) for i in range(1, n)]
    losses = [max(float(closes[i - 1]) - float(closes[i]), 0.0) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def adx(candles: Sequence[Candle], di_length: int = 14, adx_smoothing: int = 14):
    """ta.dmi(di_length, adx_smoothing) equivalent: returns (ADX, +DI, -DI)."""
    n = len(candles)
    out = [None] * n
    if di_length <= 0 or adx_smoothing <= 0 or n <= di_length:
        return out

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        current, previous = candles[i], candles[i - 1]
        up = float(current.h - previous.h)
        down = float(previous.l - current.l)
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(
            float(current.h - current.l),
            abs(float(current.h) - float(previous.c)),
            abs(float(current.l) - float(previous.c)),
        )

    tr_sm = sum(tr[1:di_length + 1])
    plus_sm = sum(plus_dm[1:di_length + 1])
    minus_sm = sum(minus_dm[1:di_length + 1])
    dx_values = []
    dx_indices = []
    di_by_index = {}
    for i in range(di_length, n):
        if i > di_length:
            tr_sm = tr_sm - tr_sm / di_length + tr[i]
            plus_sm = plus_sm - plus_sm / di_length + plus_dm[i]
            minus_sm = minus_sm - minus_sm / di_length + minus_dm[i]
        if tr_sm > 0:
            plus_di = 100.0 * plus_sm / tr_sm
            minus_di = 100.0 * minus_sm / tr_sm
            total = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / total if total else 0.0
        else:
            plus_di = minus_di = dx = 0.0
        dx_indices.append(i)
        dx_values.append(dx)
        di_by_index[i] = (plus_di, minus_di)

    if len(dx_values) < adx_smoothing:
        return out
    current_adx = sum(dx_values[:adx_smoothing]) / adx_smoothing
    first_index = dx_indices[adx_smoothing - 1]
    plus_di, minus_di = di_by_index[first_index]
    out[first_index] = (current_adx, plus_di, minus_di)
    for k in range(adx_smoothing, len(dx_values)):
        current_adx = (current_adx * (adx_smoothing - 1) + dx_values[k]) / adx_smoothing
        idx = dx_indices[k]
        plus_di, minus_di = di_by_index[idx]
        out[idx] = (current_adx, plus_di, minus_di)
    return out


def session_vwap_mode(candles: Sequence[Candle]) -> list[str]:
    """Label each session's calculation; never silently label a TWAP fallback as VWAP."""
    totals = {}
    for candle in candles:
        day = candle.ts.date()
        totals[day] = totals.get(day, 0.0) + max(0.0, float(candle.v))
    return ["VWAP" if totals.get(candle.ts.date(), 0.0) > 0 else "TWAP_FALLBACK" for candle in candles]


def session_vwap(candles: Sequence[Candle]) -> list[float | None]:
    """Session HLC3 VWAP; zero-volume sessions use a separately labelled cumulative TWAP fallback."""
    modes = session_vwap_mode(candles)
    out = []
    day = None
    pv = volume = tp_sum = 0.0
    count = 0
    mode = "VWAP"
    for candle, candle_mode in zip(candles, modes):
        current_day = candle.ts.date()
        if current_day != day:
            day = current_day
            pv = volume = tp_sum = 0.0
            count = 0
            mode = candle_mode
        typical = (float(candle.h) + float(candle.l) + float(candle.c)) / 3.0
        tp_sum += typical
        count += 1
        if candle.v > 0:
            pv += typical * float(candle.v)
            volume += float(candle.v)
        out.append(pv / volume if volume > 0 else (tp_sum / count if mode == "TWAP_FALLBACK" else None))
    return out
