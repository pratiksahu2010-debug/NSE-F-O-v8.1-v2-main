from datetime import datetime
import math
from typing import Dict, List, Optional, Tuple

from config import cfg
from models import Vote
from indicators.core import adx, atr, close_pos, ema, rsi, vwap, vol_ratio

def _hm(now: datetime):
    return (now.hour, now.minute)


def full_confidence(now: Optional[datetime]) -> float:
    """Confidence for a FULL signal. Lunch-hour chop and late-day signals (little time to work) need extra conviction."""
    need = cfg.MIN_CONFIDENCE
    if now is not None:
        lo, hi = cfg.MIDDAY_WINDOW
        if lo <= _hm(now) <= hi:
            need += cfg.MIDDAY_EXTRA_CONF
        if _hm(now) >= cfg.LATE_AFTER:
            need += cfg.LATE_EXTRA_CONF
    return need


def required_confidence(now: Optional[datetime]) -> float:
    """Lowest confidence that is sent at all: the EARLY floor when the early tier is on, else the FULL bar."""
    full = full_confidence(now)
    return min(full, cfg.EARLY_MIN_SCORE + (full - cfg.MIN_CONFIDENCE)) if cfg.EARLY_ENABLED else full


def evaluate(sources: Dict[str, float], regime: Optional[dict] = None, htf: Optional[int] = None,
             mkt: int = 0, now: Optional[datetime] = None) -> Tuple[Optional[tuple], str]:
    """Legacy 0-100 compatibility scorer. Price setup determines direction; OI/chain do not gate or score it.

    The live scanner uses score_10_point() and ALERT_SCORE_MIN. This helper mirrors the same principle:
    one numerical confidence floor, with optional market-context penalty only.
    """
    price = float(sources.get("price", 0.0) or 0.0)
    if not math.isfinite(price) or price == 0.0:
        return None, "no price setup"
    side = "CALL" if price > 0 else "PUT"
    sign = 1 if price > 0 else -1
    conf = 100.0 * abs(price)
    # Keep regime as contextual information, but do not require OI or chain direction to agree.
    rg_s = float(sources.get("regime", 0.0) or 0.0)
    conf += cfg.REGIME_BONUS * rg_s * sign
    conf = max(0.0, min(100.0, conf))
    if htf == -sign:
        conf *= cfg.HTF_OPPOSED_PENALTY
    if regime:
        if regime.get("chop"):
            conf *= cfg.CHOP_PENALTY
        if regime.get("expiry_day"):
            conf *= cfg.EXPIRY_DAY_PENALTY
    if htf == 0:
        conf *= cfg.HTF_NEUTRAL_PENALTY
    if mkt == -sign:
        conf *= cfg.MKT_OPPOSED_PENALTY
    need = required_confidence(now)
    if conf < need:
        return None, f"score {conf:.1f}/100 < {need:.1f}/100"
    return (side, round(conf, 1), 1), ""


def combine(sources: Dict[str, float], regime: Optional[dict] = None, htf: Optional[int] = None,
            mkt: int = 0, now: Optional[datetime] = None):
    return evaluate(sources, regime, htf, mkt, now)[0]


def reasons(votes: List[Vote], side: str) -> List[str]:
    sign = 1 if side == "CALL" else -1
    agreeing = sorted([v for v in votes if v.score * sign > 0], key=lambda v: -abs(v.score))
    return [v.reason for v in agreeing if v.reason]


def _cross_count(values) -> int:
    """Count directional side flips, ignoring exact-zero values."""
    signs = [1 if float(v) > 0 else -1 for v in values if float(v) != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def _breakout_expansion_confirmed(allc, sign: int) -> bool:
    """Allow a transition out of chop only on a fresh 6-bar break with range and participation expansion."""
    if len(allc) < 10:
        return False
    last = allc[-1]
    prior = allc[-7:-1]
    if len(prior) != 6:
        return False
    broke = last.c > max(c.h for c in prior) if sign > 0 else last.c < min(c.l for c in prior)
    if not broke:
        return False
    base = allc[-9:-1]
    avg_range = sum(max(0.0, c.h - c.l) for c in base) / max(1, len(base))
    if avg_range <= 0 or (last.h - last.l) < cfg.CHOP_BREAKOUT_RANGE_RATIO * avg_range:
        return False
    vr = vol_ratio(allc, 20)
    return vr is None or vr >= cfg.CHOP_BREAKOUT_VOL_RATIO


def score_10_point(
    side: str,
    session,
    allc,
    regime: Optional[dict] = None,
    htf: Optional[int] = None,
    mkt: int = 0,
    votes: Optional[List[Vote]] = None,
    htf_vwap: Optional[int] = None,
) -> dict:
    """Transparent 10-point alert score.

    Trend setups require ADX >= ADX_MIN (25); breakout/reclaim/impulse setups and reversals have
    their own higher-specificity trigger checks and configurable ADX floors (SETUP_MIN_ADX / REVERSAL_MIN_ADX). A dedicated
    REVERSAL vote uses a separate scoring profile because requiring EMA9/21 trend alignment, trend-direction RSI and
    trend-direction DI would contradict the counter-trend trigger that already passed
    stretch + RSI-extreme/turn + reversal-candle checks.
    """
    sign = 1 if side == "CALL" else -1
    vote_names = {getattr(v, "name", "") for v in (votes or [])}
    is_reversal = "REVERSAL" in vote_names
    is_trigger_setup = bool(vote_names.intersection({"ORB", "VWAP", "SQUEEZE", "MOMENTUM_EXPLOSION"}))
    reasons: list[str] = []
    rejects: list[str] = []
    components = {
        "ema_9_21": 0.0,
        "vwap": 0.0,
        "adx_di": 0.0,
        "rsi_momentum": 0.0,
        "market_regime": 0.0,
        "entry_timing": 0.0,
    }
    if len(allc) < 30 or len(session) < 3:
        return {"score": 0.0, "components": components,
                "reasons": reasons, "rejects": ["insufficient_indicator_history"],
                "metrics": {}}
    closes = [float(c.c) for c in allc]
    e9, e21 = ema(closes, cfg.EMA_FAST), ema(closes, cfg.EMA_SLOW)
    vw = vwap(session)
    rsi_values = rsi(closes, 14)
    adxv, pdi, ndi = adx(allc, 14)[-1]
    atrv = atr(allc, 14)[-1]
    last = allc[-1]
    indicator_values = (e9[-1], e21[-1], vw[-1], rsi_values[-1], rsi_values[-2], adxv, pdi, ndi, atrv)
    if not all(x is not None and math.isfinite(float(x)) for x in indicator_values) or atrv <= 0:
        return {"score": 0.0, "components": components,
                "reasons": reasons, "rejects": ["indicator_data_unavailable"],
                "metrics": {}}

    # ---- HARD DIRECTION GATE (setup-aware) ----
    # Trend-following (no trigger/reversal vote): strict confirmation for EVERY side:
    #   CALL: close > VWAP, EMA9 > EMA21, RSI in (50, RSI_CALL_MAX].  PUT: mirror image.
    # Trigger setups (ORB / VWAP reclaim / SQUEEZE / MOMENTUM_EXPLOSION): the trigger already proves a range or
    #   VWAP break with expansion, so only the VWAP side and the RSI overbought/oversold limits are hard. EMA9/21
    #   alignment and RSI 50-regime lag a breakout and are scored, not vetoed.
    # REVERSAL: the REVERSAL vote itself requires a prior EMA21 stretch, an RSI extreme and an RSI turn plus a
    #   reversal candle (strategies/price.py). Trend-side EMA/VWAP/RSI-50 hard gates contradict that thesis,
    #   so only the RSI extreme limit that the reversal setup already enforces is kept here as a backstop.
    _c, _vw, _e9, _e21 = float(last.c), float(vw[-1]), float(e9[-1]), float(e21[-1])
    _rsi = float(rsi_values[-1])
    if is_reversal:
        if sign > 0 and _rsi > cfg.RSI_CALL_MAX:
            rejects.append("rsi_overbought_for_reversal_call")
            reasons.append(f"HARD GATE: reversal CALL into RSI {_rsi:.1f} > {cfg.RSI_CALL_MAX:.0f}")
        if sign < 0 and _rsi < cfg.RSI_PUT_MIN:
            rejects.append("rsi_oversold_for_reversal_put")
            reasons.append(f"HARD GATE: reversal PUT into RSI {_rsi:.1f} < {cfg.RSI_PUT_MIN:.0f}")
    else:
        if sign * (_c - _vw) <= 0:
            rejects.append("price_wrong_side_of_vwap")
            reasons.append(f"HARD GATE: {side} needs close {'above' if sign > 0 else 'below'} VWAP (close {_c:.2f}, VWAP {_vw:.2f})")
        if not is_trigger_setup:
            if sign * (_e9 - _e21) <= 0:
                rejects.append("ema9_21_wrong_side")
                reasons.append(f"HARD GATE: {side} needs EMA9 {'>' if sign > 0 else '<'} EMA21 (EMA9 {_e9:.2f}, EMA21 {_e21:.2f})")
            if sign > 0 and not (50.0 < _rsi <= cfg.RSI_CALL_MAX):
                rejects.append("rsi_not_bullish")
                reasons.append(f"HARD GATE: CALL needs RSI between 50 and {cfg.RSI_CALL_MAX:.0f} (RSI {_rsi:.1f})")
            if sign < 0 and not (cfg.RSI_PUT_MIN <= _rsi < 50.0):
                rejects.append("rsi_not_bearish")
                reasons.append(f"HARD GATE: PUT needs RSI between {cfg.RSI_PUT_MIN:.0f} and 50 (RSI {_rsi:.1f})")
        else:
            if sign > 0 and _rsi > cfg.RSI_CALL_MAX:
                rejects.append("rsi_overbought_for_setup_call")
                reasons.append(f"HARD GATE: setup CALL into RSI {_rsi:.1f} > {cfg.RSI_CALL_MAX:.0f}")
            if sign < 0 and _rsi < cfg.RSI_PUT_MIN:
                rejects.append("rsi_oversold_for_setup_put")
                reasons.append(f"HARD GATE: setup PUT into RSI {_rsi:.1f} < {cfg.RSI_PUT_MIN:.0f}")

    # Hard chop gate: do not let a high composite score override repeated EMA/VWAP whipsaws.
    # A transition is permitted only when the current completed candle breaks the recent range with expansion.
    reg = regime or {}
    if cfg.CHOP_GATE_ENABLED:
        ema_crosses = _cross_count([e9[i] - e21[i] for i in range(max(0, len(e9) - 20), len(e9))])
        session_vw = vwap(session)
        vwap_crosses = _cross_count([c.c - session_vw[i] for i, c in enumerate(session)])
        recent_vwap_crosses = _cross_count([c.c - session_vw[i] for i, c in enumerate(session)][-12:])
        transition = _breakout_expansion_confirmed(allc, sign)
        if (reg.get("chop", False) or ema_crosses > cfg.EMA_MAX_CROSSES or
                recent_vwap_crosses > cfg.VWAP_MAX_CROSSES):
            if not transition:
                if reg.get("chop", False):
                    rejects.append("market_chop_without_expansion_breakout")
                if ema_crosses > cfg.EMA_MAX_CROSSES:
                    rejects.append(f"ema_chop_crosses_{ema_crosses}")
                if recent_vwap_crosses > cfg.VWAP_MAX_CROSSES:
                    rejects.append(f"vwap_chop_crosses_{recent_vwap_crosses}")
            else:
                reasons.append("Chop transition: fresh range breakout with expansion confirmed")
    else:
        ema_crosses = recent_vwap_crosses = 0

    # 1) EMA evidence — reversal uses reclaim/slope, not EMA9>EMA21 (which can lag the turn).
    ema_aligned = sign * (e9[-1] - e21[-1]) > 0
    ema_slope = len(e9) >= 3 and sign * (e9[-1] - e9[-3]) > 0
    price_reclaimed_ema9 = sign * (float(last.c) - float(e9[-1])) > 0
    if is_reversal:
        components["ema_9_21"] = 2.0 if price_reclaimed_ema9 and ema_slope else 1.5 if price_reclaimed_ema9 else 0.5 if ema_slope else 0.0
        reasons.append(f"Reversal EMA reclaim/slope: {components['ema_9_21']:.1f}/2.0 (EMA9 {float(e9[-1]):.2f}, EMA21 {float(e21[-1]):.2f})")
    else:
        components["ema_9_21"] = 2.0 if ema_aligned and ema_slope else 1.5 if ema_aligned else 0.0
        reasons.append(f"EMA9/EMA21 alignment: {components['ema_9_21']:.1f}/2.0 (EMA9 {float(e9[-1]):.2f}, EMA21 {float(e21[-1]):.2f})")

    # 2) VWAP evidence — reversal may trigger before a full VWAP cross, so proximity earns only partial credit.
    vwap_aligned = sign * (float(last.c) - float(vw[-1])) > 0
    vwap_slope = len(vw) >= 4 and sign * (float(vw[-1]) - float(vw[-4])) > 0
    vwap_distance_atr = abs(float(last.c) - float(vw[-1])) / float(atrv)
    if is_reversal:
        components["vwap"] = 1.5 if vwap_aligned and vwap_slope else 1.0 if vwap_aligned else 0.5 if vwap_distance_atr <= 0.5 else 0.0
        reasons.append(f"Reversal VWAP recovery/proximity: {components['vwap']:.1f}/1.5 (close {float(last.c):.2f}, VWAP {float(vw[-1]):.2f})")
    else:
        components["vwap"] = 1.5 if vwap_aligned and vwap_slope else 1.0 if vwap_aligned else 0.0
        reasons.append(f"VWAP alignment: {components['vwap']:.1f}/1.5 (close {float(last.c):.2f}, VWAP {float(vw[-1]):.2f})")

    # 3) ADX + DI. Trend-following needs ADX_MIN (25); trigger setups use SETUP_MIN_ADX; reversals use REVERSAL_MIN_ADX.
    if is_reversal:
        adx_floor, reject_code, label = float(cfg.REVERSAL_MIN_ADX), "reversal_adx_below_min", "Reversal"
    elif is_trigger_setup:
        adx_floor, reject_code, label = float(cfg.SETUP_MIN_ADX), "setup_adx_below_min", "Setup"
    else:
        adx_floor, reject_code, label = float(cfg.ADX_MIN), "adx_below_25", "Trend"

    if float(adxv) < adx_floor:
        rejects.append(reject_code)
        reasons.append(f"{label} ADX hard gate failed: {float(adxv):.1f} < {adx_floor:.1f} (+DI {float(pdi):.1f}/-DI {float(ndi):.1f})")
    elif sign * (float(pdi) - float(ndi)) > 0:
        components["adx_di"] = 1.5 if sign * (float(pdi) - float(ndi)) >= cfg.DI_MIN_GAP else 1.0
        reasons.append(f"{label} ADX/DI confirmation: {components['adx_di']:.1f}/1.5 (ADX {float(adxv):.1f}, +DI {float(pdi):.1f}/-DI {float(ndi):.1f})")
    elif is_reversal:
        components["adx_di"] = 0.75
        reasons.append(f"Reversal ADX context: 0.8/1.5 (ADX {float(adxv):.1f}; DI may lag turns)")
    else:
        reasons.append(f"ADX passed but DI direction conflicts: 0.0/1.5 (ADX {float(adxv):.1f}, +DI {float(pdi):.1f}/-DI {float(ndi):.1f})")

    # 4) Trend RSI uses 50-regime + directional change. Reversal RSI uses the turn already required by its setup.
    r_now, r_prev = float(rsi_values[-1]), float(rsi_values[-2])
    rsi_directional = sign * (r_now - 50.0) > 0
    rsi_rising = sign * (r_now - r_prev) > 0
    rsi_in_zone = (cfg.RSI_BULL_MIN <= r_now <= cfg.RSI_CALL_MAX) if sign > 0 else (cfg.RSI_PUT_MIN <= r_now <= cfg.RSI_BEAR_MAX)
    if is_reversal:
        components["rsi_momentum"] = 2.0 if rsi_rising else 0.0
        reasons.append(f"Reversal RSI turn: {components['rsi_momentum']:.1f}/2.0 (RSI {r_now:.1f}, previous {r_prev:.1f})")
    else:
        if rsi_in_zone and rsi_rising:
            components["rsi_momentum"] = 2.0
        elif rsi_in_zone:
            components["rsi_momentum"] = 1.5
        elif rsi_directional and rsi_rising:
            components["rsi_momentum"] = 1.0
        elif rsi_directional:
            components["rsi_momentum"] = 0.5
        reasons.append(f"RSI momentum: {components['rsi_momentum']:.1f}/2.0 (RSI {r_now:.1f}, previous {r_prev:.1f})")

    # 5) Market regime — 2.0 points. Chop and opposing higher-timeframe/market context reduce points.
    context_opposed = (htf == -sign) or (mkt == -sign)
    regime_score = reg.get("score")
    try:
        regime_score_value = float(regime_score)
        regime_known = isinstance(regime, dict) and math.isfinite(regime_score_value)
    except (TypeError, ValueError, OverflowError):
        regime_score_value, regime_known = 0.0, False
    directional_regime = sign * regime_score_value if regime_known else 0.0
    if reg.get("chop", False):
        components["market_regime"] = 0.0
    elif context_opposed:
        components["market_regime"] = 0.75
    elif not regime_known:
        # Missing/invalid regime evidence must not receive the same points as a confirmed supportive regime.
        components["market_regime"] = 0.5
    else:
        components["market_regime"] = 1.5
        if htf == sign or mkt == sign:
            components["market_regime"] = 1.75
        if directional_regime >= 0.20:
            components["market_regime"] = 2.0
        elif directional_regime <= -0.20:
            components["market_regime"] = max(0.0, components["market_regime"] - 0.5)
    regime_note = ("chop" if reg.get("chop", False) else
                   "opposing context" if context_opposed else
                   "regime unavailable/invalid" if not regime_known else
                   "supportive/neutral context")
    # 15m session VWAP is supporting evidence inside the existing regime
    # component, not a new score gate. It can nudge confidence either way but
    # can never reject a setup or push the component outside its 0..2 range.
    if htf_vwap in (-1, 1):
        if htf_vwap == sign:
            components["market_regime"] = min(2.0, components["market_regime"] + 0.15)
            reasons.append("15m VWAP context aligned (+0.15 score support)")
        else:
            components["market_regime"] = max(0.0, components["market_regime"] - 0.15)
            reasons.append("15m VWAP context opposed (-0.15 score support; not a veto)")
    else:
        reasons.append("15m VWAP context neutral/unavailable (no score change)")
    reasons.append(f"Market regime: {components['market_regime']:.2f}/2.0 ({regime_note})")

    # 6) Entry timing — 1.0 point. Stage 1 already requires a fresh trigger; penalize weak close / chase.
    cp = close_pos(last)
    directional_close = float(cp) if sign > 0 else 1.0 - float(cp)
    ema9_extension = abs(float(last.c) - float(e9[-1])) / float(atrv)
    has_trigger = bool(votes)
    if has_trigger and directional_close >= cfg.CLOSE_POS_MIN and ema9_extension <= cfg.CHASE_ATR:
        components["entry_timing"] = 1.0
    elif has_trigger and ema9_extension <= cfg.MAX_EXTENSION_ATR:
        components["entry_timing"] = 0.5
    else:
        components["entry_timing"] = 0.0
    reasons.append(f"Entry timing: {components['entry_timing']:.1f}/1.0 (EMA9 extension {ema9_extension:.2f} ATR)")

    total = round(sum(components.values()), 2)
    metrics = {
        "adx": float(adxv), "plus_di": float(pdi), "minus_di": float(ndi),
        "ema9": float(e9[-1]), "ema21": float(e21[-1]), "vwap": float(vw[-1]),
        "rsi": float(rsi_values[-1]), "rsi_previous": float(rsi_values[-2]), "ema9_extension_atr": round(ema9_extension, 3),
        "directional_close_location": round(directional_close, 3),
        "ema_crosses_20": int(ema_crosses), "vwap_crosses_12": int(recent_vwap_crosses),
        "score_components": dict(components), "score_max": 10.0,
        "htf_vwap_bias": int(htf_vwap) if htf_vwap in (-1, 0, 1) else None,
        "alert_score_minimum": float(getattr(cfg, "ALERT_SCORE_MIN", 8.0)),
    }
    if total < float(getattr(cfg, "ALERT_SCORE_MIN", 8.0)):
        reasons.append(f"Score below alert threshold: {total:.2f}/10 < {cfg.ALERT_SCORE_MIN:.1f}/10")
    return {"score": total, "components": components, "reasons": reasons,
            "rejects": rejects, "metrics": metrics}
