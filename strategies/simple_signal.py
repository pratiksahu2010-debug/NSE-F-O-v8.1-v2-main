"""Simple closed-bar CALL / PUT rule: EMA 9/21 + session VWAP + RSI(14) + ADX(14) >= threshold.

The rule is deliberately small so that it can be read, tested and tuned in one place.

CALL needs, on the latest *closed* 5-minute candle, all four of:
  1. EMA9 above EMA21
  2. close above session VWAP
  3. RSI(14) inside [rsi_call_min, rsi_call_max]   (momentum up, not an overbought spike)
  4. ADX(14) >= adx_min                              (a real trend, default 25)

PUT is the exact mirror (EMA9 below EMA21, close below VWAP, RSI inside [rsi_put_min, rsi_put_max], same ADX).

Freshness: a state that has been true for a long time is a late entry, so the rule only fires while the full
condition set has been true for at most `fresh_bars` consecutive candles (default 3 = the move is at most ~15 minutes
old). That also gives a retry window: if the option chain or contract check fails on the first candle, the next two
candles can still produce the alert. De-duplication across scans is handled by the scanner (one open signal per
underlying and a persisted per-candle key).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from indicators.core import adx, atr, ema, rsi, vwap
from models import Candle

SETUP_TYPE = "TREND_ENTRY"

_FLIP_TEXT = {
    "CALL": {"ema": "EMA9 crossed above EMA21", "vwap": "price reclaimed VWAP",
             "rsi": "RSI entered the bullish zone", "adx": "ADX reached the trend threshold"},
    "PUT": {"ema": "EMA9 crossed below EMA21", "vwap": "price lost VWAP",
            "rsi": "RSI entered the bearish zone", "adx": "ADX reached the trend threshold"},
}


def _empty(reason: str, failures: Optional[List[str]] = None, metrics: Optional[dict] = None,
           gate_results: Optional[dict] = None) -> dict:
    return {
        "side": 0, "score": 0.0, "setup_type": None, "reason": reason,
        "gate_results": gate_results or {}, "gate_failures": failures or [], "metrics": metrics or {},
    }


def evaluate_simple(
    session: List[Candle],
    all_candles: List[Candle],
    *,
    adx_min: float = 25.0,
    rsi_call_min: float = 55.0,
    rsi_call_max: float = 75.0,
    rsi_put_max: float = 45.0,
    rsi_put_min: float = 25.0,
    fresh_bars: int = 3,
    min_bars: int = 40,
    min_session_bars: int = 3,
    max_entry_extension_atr: float = 1.5,
    context_15m: int = 0,
) -> dict:
    """Evaluate the latest closed candle. Returns the same dict shape as the other strategy engines."""
    fresh_bars = max(1, int(fresh_bars))
    if len(session) < int(min_session_bars):
        return _empty("few session bars", ["session_bars"],
                      {"session_bars": len(session), "required_session_bars": int(min_session_bars)})
    if len(all_candles) < max(int(min_bars), 2 * 14 + 1 + fresh_bars):
        # ADX(14) needs 2*14+1 candles before it reports anything but zeros.
        return _empty("insufficient indicator history", ["history"],
                      {"history_bars": len(all_candles), "required_bars": int(min_bars)})

    closes = [float(c.c) for c in all_candles]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    rsi_values = rsi(closes, 14)
    adx_values = adx(all_candles, 14)
    atr_values = atr(all_candles, 14)
    vw_by_ts = {c.ts: v for c, v in zip(session, vwap(session))}

    n_bars = len(all_candles)

    def components(side: str, i: int) -> Dict[str, bool]:
        """Per-condition truth values for `side` at absolute index i (negative indexes allowed)."""
        bar = all_candles[i]
        vw = vw_by_ts.get(bar.ts)            # None for yesterday's bars: VWAP is a session measure
        ax = float(adx_values[i][0])
        r = float(rsi_values[i])
        if side == "CALL":
            return {"ema": e9[i] > e21[i], "vwap": vw is not None and bar.c > vw,
                    "rsi": float(rsi_call_min) <= r <= float(rsi_call_max), "adx": ax >= float(adx_min)}
        return {"ema": e9[i] < e21[i], "vwap": vw is not None and bar.c < vw,
                "rsi": float(rsi_put_min) <= r <= float(rsi_put_max), "adx": ax >= float(adx_min)}

    last = all_candles[-1]
    vw_now = vw_by_ts.get(last.ts)
    if vw_now is None or vw_now <= 0:
        return _empty("VWAP unavailable for the latest candle", ["vwap_unavailable"])
    ax, pdi, ndi = (float(x) for x in adx_values[-1])
    rv = float(rsi_values[-1])
    a_now = float(atr_values[-1]) if atr_values else 0.0
    metrics = {
        "price": float(last.c), "ema9": float(e9[-1]), "ema21": float(e21[-1]), "vwap": float(vw_now),
        "rsi": rv, "adx": ax, "plus_di": pdi, "minus_di": ndi, "atr": a_now,
        "thresholds": {"adx_min": float(adx_min), "rsi_call": [float(rsi_call_min), float(rsi_call_max)],
                       "rsi_put": [float(rsi_put_min), float(rsi_put_max)], "fresh_bars": fresh_bars},
    }
    snapshot = (f"EMA9 {e9[-1]:.2f} {'>' if e9[-1] > e21[-1] else '<'} EMA21 {e21[-1]:.2f}, "
                f"{'above' if last.c > vw_now else 'below'} VWAP {vw_now:.2f}, RSI {rv:.0f}, "
                f"ADX {ax:.0f} (+DI {pdi:.0f}/-DI {ndi:.0f}), ATR {a_now:.2f}")
    if a_now <= 0:
        return _empty("ATR unavailable", ["atr_unavailable"], metrics)
    extension_call = (float(last.c) - float(e9[-1])) / a_now
    extension_put = (float(e9[-1]) - float(last.c)) / a_now
    metrics["ema9_extension_atr"] = round(max(extension_call, extension_put), 4)
    metrics["max_entry_extension_atr"] = float(max_entry_extension_atr)

    window = min(fresh_bars + 1, n_bars)
    gate_results: Dict[str, Dict[str, bool]] = {}
    not_fresh: Dict[str, int] = {}
    for side in ("CALL", "PUT"):
        history = [components(side, -k) for k in range(window, 0, -1)]      # oldest ... newest
        gate_results[side] = history[-1]
        if not all(history[-1].values()):
            continue
        context_ok = int(context_15m or 0) >= 0 if side == "CALL" else int(context_15m or 0) <= 0
        gate_results[side]["context_15m"] = context_ok
        if not context_ok:
            not_fresh[side] = -2
            continue
        run = 0
        for k in range(1, n_bars + 1):                                     # consecutive true candles ending now
            if not all(components(side, -k).values()):
                break
            run += 1
            if run > fresh_bars:
                break
        if run > fresh_bars:
            not_fresh[side] = run
            continue
        directional_extension = extension_call if side == "CALL" else extension_put
        if directional_extension > float(max_entry_extension_atr):
            not_fresh[side] = -1
            continue
        flipped = [name for name, ok in components(side, -(run + 1)).items() if not ok] if run < n_bars else []
        trigger = [_FLIP_TEXT[side][name] for name in flipped] or ["all conditions aligned"]
        metrics.update({"bars_in_signal": run, "trigger": trigger})
        direction = "LONG" if side == "CALL" else "SHORT"
        reason = (f"{direction} trend entry: {'; '.join(trigger)} | {snapshot} | "
                  f"ADX >= {float(adx_min):g}, signal age {run} candle(s)")
        return {"side": 1 if side == "CALL" else -1, "score": 1.0, "setup_type": SETUP_TYPE,
                "reason": reason, "gate_results": gate_results, "gate_failures": [], "metrics": metrics}

    # No trigger: report the failing conditions of the side that is closest to firing.
    def passed(side: str) -> int:
        return sum(1 for ok in gate_results[side].values() if ok)

    best = "CALL" if passed("CALL") >= passed("PUT") else "PUT"
    failures = [f"{best}:{name}" for name, ok in gate_results[best].items() if not ok]
    for side, run in not_fresh.items():
        if run == -1:
            failures.append(f"{side}:extended_from_ema9")
            metrics.setdefault("extension_rejections", []).append(side)
        elif run == -2:
            failures.append(f"{side}:15m_context_opposed")
        else:
            failures.append(f"{side}:not_fresh")
            metrics.setdefault("stale_signal_bars", {})[side] = run
        best = side
    reason = "no trigger | " + snapshot
    if failures:
        reason += " | " + ",".join(failures)
    return _empty(reason, failures, metrics, gate_results)


def evaluate_simple_intrabar(
    session: List[Candle],
    completed_candles: List[Candle],
    partial_candle: Candle,
    *,
    adx_min: float = 25.0,
    rsi_call_min: float = 55.0,
    rsi_call_max: float = 75.0,
    rsi_put_max: float = 45.0,
    rsi_put_min: float = 25.0,
    fresh_bars: int = 3,
    min_bars: int = 35,
    min_session_bars: int = 3,
    max_entry_extension_atr: float = 1.5,
    context_15m: int = 0,
) -> dict:
    """Evaluate the SIMPLE EMA/VWAP/RSI/ADX rules on a live partial 5m candle.

    The partial candle is never inserted into persisted/closed history. No MACD,
    candle-colour/body, DI-direction, sector or NIFTY agreement gate is added.
    """
    if len(session) < int(min_session_bars):
        return _empty("few session bars", ["session_bars"])
    if len(completed_candles) < int(min_bars):
        return _empty("insufficient indicator history", ["history"])
    if partial_candle.ts <= completed_candles[-1].ts:
        return _empty("live candle timestamp is not newer than closed history", ["live_timestamp"])

    closed_result = evaluate_simple(
        session, completed_candles, adx_min=adx_min,
        rsi_call_min=rsi_call_min, rsi_call_max=rsi_call_max,
        rsi_put_max=rsi_put_max, rsi_put_min=rsi_put_min,
        fresh_bars=fresh_bars, min_bars=min_bars,
        min_session_bars=min_session_bars,
        max_entry_extension_atr=max_entry_extension_atr,
        context_15m=context_15m,
    )
    combined = list(completed_candles) + [partial_candle]
    closes = [float(c.c) for c in combined]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    rsi_values, adx_values, atr_values = rsi(closes, 14), adx(combined, 14), atr(combined, 14)
    vw_values = vwap(session)
    if not all((e9, e21, rsi_values, adx_values, atr_values, vw_values)):
        return _empty("live indicator series unavailable", ["indicator_warmup"])
    try:
        ax, pdi, ndi = (float(x) for x in adx_values[-1])
        rv = float(rsi_values[-1])
        av = float(atr_values[-1])
        vw = float(vw_values[-1])
    except (TypeError, ValueError, IndexError):
        return _empty("live indicator values unavailable", ["indicator_values"])
    if av <= 0 or vw <= 0:
        return _empty("live ATR/VWAP unavailable", ["live_reference"])
    price = float(partial_candle.c)
    ext_call = (price - float(e9[-1])) / av
    ext_put = (float(e9[-1]) - price) / av
    common = {
        "price": price, "ema9": float(e9[-1]), "ema21": float(e21[-1]),
        "vwap": vw, "rsi": rv, "adx": ax, "plus_di": pdi,
        "minus_di": ndi, "atr": av,
        "ema9_extension_atr": round(max(ext_call, ext_put), 4),
        "max_entry_extension_atr": float(max_entry_extension_atr),
        "context_15m": int(context_15m or 0),
        "mode": "INTRABAR",
    }

    # Do not re-enter a state that has already remained fully aligned beyond
    # the configured freshness window on completed bars.
    stale = (closed_result.get("metrics") or {}).get("stale_signal_bars", {})
    candidates = (
        ("CALL", 1, e9[-1] > e21[-1] and price > vw and
         float(rsi_call_min) <= rv <= float(rsi_call_max) and ax >= float(adx_min)
         and int(context_15m or 0) >= 0,
         ext_call, "LONG"),
        ("PUT", -1, e9[-1] < e21[-1] and price < vw and
         float(rsi_put_min) <= rv <= float(rsi_put_max) and ax >= float(adx_min)
         and int(context_15m or 0) <= 0,
         ext_put, "SHORT"),
    )
    for side, sign, aligned, extension, label in candidates:
        if not aligned or side in stale or extension > float(max_entry_extension_atr):
            continue
        reason = (f"EARLY {label} Normal trigger: EMA9/21 aligned, price on correct VWAP side, "
                  f"RSI {rv:.1f}, ADX {ax:.1f}; live 5m candle, extension {extension:.2f} ATR")
        return {
            "side": sign, "score": 1.0, "setup_type": SETUP_TYPE,
            "reason": reason, "gate_results": {side: {"ema": True, "vwap": True, "rsi": True, "adx": True}},
            "gate_failures": [], "metrics": {**common, "trigger_edge": float(e9[-1])},
        }
    failures = []
    for side, sign, aligned, extension, _ in candidates:
        if not aligned:
            if side == "CALL" and int(context_15m or 0) < 0 or side == "PUT" and int(context_15m or 0) > 0:
                failures.append(f"{side}:15m_context_opposed")
            else:
                failures.extend([f"{side}:ema_vwap_rsi_adx_not_aligned"])
        elif side in stale:
            failures.append(f"{side}:not_fresh")
        elif extension > float(max_entry_extension_atr):
            failures.append(f"{side}:extended_from_ema9")
    return _empty("no live SIMPLE trigger", failures, common)
