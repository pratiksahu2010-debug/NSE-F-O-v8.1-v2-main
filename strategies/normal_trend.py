"""Independent 5-minute Normal Trend engine.

The engine uses only the agreed signal indicators: EMA(9/21), MACD(12/26/9),
session VWAP, RSI(14), and ADX(14) with +DI/-DI. ATR is used only as a
price-distance ruler for pullback/entry validation. The detector returns a
5-minute setup; the scanner applies the configured completed-15-minute context
policy (directional hard gate in V3, neutral-allowed in legacy mode).
"""
from __future__ import annotations

from indicators.core import adx, atr, ema, macd, rsi, vwap, vol_ratio
from models import Candle


def _empty(reason: str, gate_results=None, gate_failures=None, metrics=None) -> dict:
    return {
        "side": 0,
        "score": 0.0,
        "setup_type": None,
        "reason": reason,
        "gate_results": gate_results or {},
        "gate_failures": gate_failures or [],
        "metrics": metrics or {},
    }


def evaluate_normal_trend_setup(
    session: list[Candle],
    all_candles: list[Candle],
    current_candle: Candle | None = None,
    *,
    current_in_history: bool = True,
    adx_min: float = 20.0,
    adx_established: float = 25.0,
    di_min_gap: float = 3.0,
    rsi_long_min: float = 48.0,
    rsi_short_max: float = 52.0,
    candle_body_min: float = 0.35,
    close_pos_min: float = 0.62,
    pullback_atr: float = 0.20,
    max_entry_extension_atr: float = 1.50,
    breakout_lookback: int = 6,
    rvol_min: float = 0.0,
) -> dict:
    """Evaluate the same three Normal Trend setups on closed or live partial bars.

    V3 profile: EMA/VWAP/RSI/ADX/DI and participation drive direction. MACD is
    retained as a diagnostic only; it is not a mandatory confirmation gate.
    """
    if not all_candles or current_candle is None and not all_candles:
        return _empty("missing candle history")
    current = current_candle or all_candles[-1]
    indicator_candles = list(all_candles)
    if not current_in_history:
        indicator_candles.append(current)
    elif indicator_candles[-1].ts != current.ts:
        indicator_candles.append(current)

    if len(indicator_candles) < 40 or len(session) < 3:
        return _empty(
            "insufficient indicator history",
            metrics={"history_bars": len(indicator_candles), "session_bars": len(session)},
        )

    closes = [float(c.c) for c in indicator_candles]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    # MACD is optional diagnostics only. A missing/invalid MACD series must not
    # veto an otherwise valid NORMAL setup.
    try:
        macd_line, macd_signal, hist = macd(closes, 12, 26, 9)
        macd_value = float(macd_line[-1]) if macd_line else 0.0
        macd_signal_value = float(macd_signal[-1]) if macd_signal else 0.0
        mh = float(hist[-1]) if hist else 0.0
        prior_mh = float(hist[-2]) if len(hist) > 1 else mh
    except (TypeError, ValueError, IndexError, OverflowError):
        macd_value = macd_signal_value = mh = prior_mh = 0.0
    rsi_values = rsi(closes, 14)
    adx_values = adx(indicator_candles, 14)
    vwap_values = vwap(session)
    atr_values = atr(indicator_candles, 14)
    if not all((e9, e21, rsi_values, adx_values, vwap_values, atr_values)):
        return _empty("indicator series unavailable")
    if len(rsi_values) < 2 or len(adx_values) < 2 or len(e9) < 2:
        return _empty("indicator warm-up incomplete")

    try:
        ax, plus_di, minus_di = (float(x) for x in adx_values[-1])
        prior_adx = float(adx_values[-2][0])
        rv, prior_rsi = float(rsi_values[-1]), float(rsi_values[-2])
        price, vw, atr_value = float(current.c), float(vwap_values[-1]), float(atr_values[-1])
        fast, slow = float(e9[-1]), float(e21[-1])
        prior_fast = float(e9[-2])
    except (TypeError, ValueError, IndexError, OverflowError):
        return _empty("invalid indicator values")
    if not all(map(lambda x: x == x and abs(x) != float("inf"),
                   (ax, plus_di, minus_di, prior_adx, rv, prior_rsi, mh, prior_mh,
                    price, vw, atr_value, fast, slow, prior_fast))):
        return _empty("non-finite indicator values")
    if price <= 0 or vw <= 0 or atr_value <= 0:
        return _empty("VWAP or ATR unavailable")

    previous = indicator_candles[-2]
    relative_volume = vol_ratio(indicator_candles, 20)
    lookback = max(2, int(breakout_lookback))
    prior_bars = indicator_candles[-(lookback + 1):-1]
    prior_high = max((float(c.h) for c in prior_bars), default=float(previous.h))
    prior_low = min((float(c.l) for c in prior_bars), default=float(previous.l))
    candle_range = max(float(current.h) - float(current.l), 1e-9)
    body_fraction = abs(float(current.c) - float(current.o)) / candle_range
    close_position = (float(current.c) - float(current.l)) / candle_range
    adx_ok = ax >= float(adx_min) and (ax >= float(adx_established) or ax > prior_adx)

    call_gates = {
        "adx_strength": adx_ok,
        "relative_volume": relative_volume is None or relative_volume >= float(rvol_min),
        "directional_di": plus_di > minus_di + float(di_min_gap),
        "ema_alignment": fast > slow,
        "above_vwap": price > vw,
        "rsi_direction": rv > float(rsi_long_min) and (rv >= 50.0 or rv >= prior_rsi),
        "macd_supporting": mh >= 0.0 or mh > prior_mh,
    }
    put_gates = {
        "adx_strength": adx_ok,
        "relative_volume": relative_volume is None or relative_volume >= float(rvol_min),
        "directional_di": minus_di > plus_di + float(di_min_gap),
        "ema_alignment": fast < slow,
        "below_vwap": price < vw,
        "rsi_direction": rv < float(rsi_short_max) and (rv <= 50.0 or rv <= prior_rsi),
        "macd_supporting": mh <= 0.0 or mh < prior_mh,
    }

    strong_call = current.c > current.o and body_fraction >= float(candle_body_min) and close_position >= float(close_pos_min)
    strong_put = current.c < current.o and body_fraction >= float(candle_body_min) and close_position <= 1.0 - float(close_pos_min)
    call_breakout = current.c > prior_high
    put_breakout = current.c < prior_low
    call_pullback = (
        current.l <= fast + float(pullback_atr) * atr_value
        and current.c > fast
    ) or (
        previous.l <= prior_fast + float(pullback_atr) * atr_value
        and current.c > fast and current.c > previous.c
    )
    put_pullback = (
        current.h >= fast - float(pullback_atr) * atr_value
        and current.c < fast
    ) or (
        previous.h >= prior_fast - float(pullback_atr) * atr_value
        and current.c < fast and current.c < previous.c
    )
    early_call = (
        ax < float(adx_established) and ax > prior_adx
        and current.c > fast
    )
    early_put = (
        ax < float(adx_established) and ax > prior_adx
        and current.c < fast
    )

    call_gates.update({
        "breakout_trigger": call_breakout,
        "pullback_resumption": call_pullback,
        "early_trend_trigger": early_call,
    })
    put_gates.update({
        "breakdown_trigger": put_breakout,
        "pullback_resumption": put_pullback,
        "early_trend_trigger": early_put,
    })

    call_common = all(call_gates[k] for k in (
        "adx_strength", "relative_volume", "directional_di", "ema_alignment", "above_vwap",
        "rsi_direction",
    ))
    put_common = all(put_gates[k] for k in (
        "adx_strength", "relative_volume", "directional_di", "ema_alignment", "below_vwap",
        "rsi_direction",
    ))
    call_setup = ("MOMENTUM_BREAKOUT" if call_breakout else
                  "TREND_PULLBACK" if call_pullback else
                  "EARLY_TREND" if early_call else None) if call_common else None
    put_setup = ("MOMENTUM_BREAKOUT" if put_breakout else
                 "TREND_PULLBACK" if put_pullback else
                 "EARLY_TREND" if early_put else None) if put_common else None

    side = 1 if call_setup else -1 if put_setup else 0
    setup_type = call_setup or put_setup
    trigger_edge = (
        prior_high if call_setup == "MOMENTUM_BREAKOUT" else
        prior_low if put_setup == "MOMENTUM_BREAKOUT" else
        fast
    ) if side else None
    gate_results = {"CALL": call_gates, "PUT": put_gates}
    failures = []
    if side:
        extension_atr = side * (price - float(trigger_edge)) / atr_value
        direction_key = "CALL" if side > 0 else "PUT"
        extension_ok = -0.02 <= extension_atr <= float(max_entry_extension_atr)
        gate_results[direction_key]["entry_extension"] = extension_ok
        if not extension_ok:
            failures.append(f"{direction_key}:entry_extension")
            side = 0
            setup_type = None
            trigger_edge = None
    else:
        extension_atr = None
    if not call_setup:
        failures.extend(f"CALL:{name}" for name, passed in call_gates.items() if not passed)
    if not put_setup:
        failures.extend(f"PUT:{name}" for name, passed in put_gates.items() if not passed)

    metrics = {
        "ema9": fast, "ema21": slow, "macd": macd_value,
        "macd_signal": macd_signal_value, "macd_histogram": mh,
        "previous_macd_histogram": prior_mh, "vwap": vw, "rsi": rv,
        "previous_rsi": prior_rsi, "adx": ax, "previous_adx": prior_adx,
        "plus_di": plus_di, "minus_di": minus_di, "atr": atr_value,
        "relative_volume": relative_volume,
        "candle_body_fraction": round(body_fraction, 4),
        "candle_close_position": round(close_position, 4),
        "prior_high": prior_high, "prior_low": prior_low,
        "trigger_edge": trigger_edge, "setup_type": setup_type,
        "entry_extension_atr": round(extension_atr, 4) if extension_atr is not None else None,
        "gate_results": gate_results, "gate_failures": failures,
    }
    if not side:
        call_failed = [k for k, passed in call_gates.items() if not passed]
        put_failed = [k for k, passed in put_gates.items() if not passed]
        reason = "no Normal Trend trigger"
        if call_failed:
            reason += " | CALL: " + ",".join(call_failed)
        if put_failed:
            reason += " | PUT: " + ",".join(put_failed)
        return _empty(reason, gate_results, failures, metrics)

    direction = "LONG" if side > 0 else "SHORT"
    reason = (
        f"{direction} {setup_type} | EMA9, EMA21, VWAP, RSI and ADX/+DI alignment "
        f"confirmed; ADX={ax:.1f}, DI gap={abs(plus_di - minus_di):.1f}"
    )
    return {
        "side": side, "score": 1.0, "setup_type": setup_type,
        "reason": reason, "gate_results": gate_results,
        "gate_failures": [], "metrics": metrics,
    }


def evaluate_normal_trend(
    session: list[Candle],
    all_candles: list[Candle],
    adx_min: float = 20.0,
    **kwargs,
) -> dict:
    """Evaluate the latest completed 5-minute candle."""
    return evaluate_normal_trend_setup(
        session, all_candles, all_candles[-1] if all_candles else None,
        current_in_history=True, adx_min=adx_min, **kwargs,
    )


def evaluate_normal_trend_intrabar(
    session: list[Candle],
    completed_candles: list[Candle],
    partial_candle: Candle,
    **kwargs,
) -> dict:
    """Evaluate the live partial candle using the same Normal Trend gates."""
    return evaluate_normal_trend_setup(
        session, completed_candles, partial_candle,
        current_in_history=False, **kwargs,
    )


__all__ = [
    "evaluate_normal_trend",
    "evaluate_normal_trend_intrabar",
    "evaluate_normal_trend_setup",
]
