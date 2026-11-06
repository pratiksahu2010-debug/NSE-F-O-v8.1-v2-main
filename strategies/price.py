"""Underlying (spot) strategy on completed 5-minute candles. Score: +1 = CALL, -1 = PUT.

Essential trend score (0-100): EMA 9/21 (25), session VWAP (20), RSI momentum (20), ADX/+DI/-DI (15),
relative volume (15), and ATR volatility adequacy (5). MACD is deliberately not a hard gate or a scoring vote;
its lag and overlap with RSI/price momentum can delay entries. Candle structure, breakout freshness and extension
checks provide the trigger/timing confirmation.

A fresh trigger is required: recent EMA cross, pullback-resume at EMA9, or a break of the last N-candle high/low.

REVERSAL is a separate counter-trend setup requiring a prior EMA21 stretch, an RSI extreme and turn, a strong
reversal candle breaking the previous candle's extreme, and confirmation from structure/participation.
"""
from typing import List, Optional, Tuple

from config import cfg
from indicators.core import (adx, atr, bars_since_cross, body_frac, close_pos, ema, rsi, vol_ratio, vwap)
from models import Candle, Vote
from strategies.setups import Ctx, score_orb, score_squeeze, score_vwap
from strategies.momentum_explosion import score_momentum_explosion


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _trigger(sgn: int, allc: List[Candle], e9, e21, a: float) -> Optional[str]:
    last, prev = allc[-1], allc[-2]
    cross = bars_since_cross(e9, e21, cfg.CROSS_FRESH_BARS)
    if cross and cross[0] == sgn:
        return "EMA9/21 cross" + (" this candle" if cross[1] == 0 else f" {cross[1]} candle(s) ago")
    n = cfg.BREAKOUT_BARS
    window = allc[-(n + 1):-1]
    if len(window) == n:
        if sgn > 0 and last.c > max(c.h for c in window):
            return f"close above {n}-candle high"
        if sgn < 0 and last.c < min(c.l for c in window):
            return f"close below {n}-candle low"
    # pullback to EMA9 inside a running trend, resumed by the latest candle
    dip = any((sgn > 0 and allc[-i].l <= e9[-i] + cfg.PB_TOUCH_ATR * a) or
              (sgn < 0 and allc[-i].h >= e9[-i] - cfg.PB_TOUCH_ATR * a) for i in (2, 3, 4))
    held = all(sgn * (allc[-i].c - e21[-i]) > 0 for i in range(1, 5))
    if dip and held and sgn * (last.c - prev.c) > 0 and sgn * (last.c - e9[-1]) > 0:
        return "pullback to EMA9 resumed"
    return None


def score_side(sgn: int, session: List[Candle], allc: List[Candle]) -> Tuple[float, str, dict]:
    """(points 0-100, trigger/why-not text, detail). points == 0 means a hard gate failed (text says which)."""
    closes = [c.c for c in allc]
    last = allc[-1]
    a = atr(allc, 14)[-1]
    if a <= 0 or last.c <= 0:
        return 0.0, "no ATR", {}
    side = "up" if sgn > 0 else "down"

    e9, e21 = ema(closes, cfg.EMA_FAST), ema(closes, cfg.EMA_SLOW)
    sep = sgn * (e9[-1] - e21[-1]) / a
    if sep <= 0:
        return 0.0, f"EMA9/21 not {side}", {}
    vw = vwap(session)
    if sgn * (last.c - vw[-1]) <= 0:
        return 0.0, "wrong side of VWAP", {}
    rv = rsi(closes, 14)
    r, r_prev = rv[-1], rv[-2]
    if (sgn > 0 and r > cfg.RSI_CALL_MAX) or (sgn < 0 and r < cfg.RSI_PUT_MIN):
        return 0.0, f"RSI exhausted ({r:.0f})", {}
    ext = sgn * (last.c - e9[-1]) / a
    if ext > cfg.MAX_EXTENSION_ATR:
        return 0.0, f"over-extended ({ext:.1f} ATR from EMA9)", {}
    atr_pct = a / last.c * 100.0
    if atr_pct < cfg.MIN_ATR_PCT:
        return 0.0, f"ATR too small ({atr_pct:.2f}%)", {}

    adxv, pdi, ndi = adx(allc, 14)[-1]
    if adxv < cfg.ADX_RISING_FLOOR:
        return 0.0, f"ADX too low ({adxv:.0f}): no trend to follow", {}
    # ---- essential-indicator points: trend, value, momentum, strength, participation, volatility ----
    pts = 0.0
    # EMA 25: aligned (8) + EMA9 slope (4) + separation (up to 13)
    pts += 8 + (4 if sgn * (e9[-1] - e9[-3]) > 0 else 0) + 13 * _clamp(sep / 0.6, 0, 1)
    # VWAP 20: right side (5) + VWAP slope (3) + previous close also on the right side (3) + distance (up to 9)
    pts += 5 + (3 if len(vw) >= 4 and sgn * (vw[-1] - vw[-4]) > 0 else 0) + (3 if sgn * (allc[-2].c - vw[-2]) > 0 else 0)
    pts += 9 * _clamp(sgn * (last.c - vw[-1]) / a / 0.8, 0, 1)
    # RSI 20: momentum zone (4) + moving in the trade direction (5) + strength (up to 11)
    # RSI 20: in the momentum zone (4) + moving the right way (5) + strength (up to 11)
    in_zone = (cfg.RSI_BULL_MIN <= r <= cfg.RSI_CALL_MAX) if sgn > 0 else (cfg.RSI_PUT_MIN <= r <= cfg.RSI_BEAR_MAX)
    pts += (4 if in_zone else 0) + (5 if sgn * (r - r_prev) > 0 else 0) + 11 * _clamp(sgn * (r - 50) / 15, 0, 1)
    # ADX 15: trend strength (up to 12) + DI lead (3)
    if adxv >= cfg.ADX_MIN:
        pts += 6 + 6 * _clamp((adxv - cfg.ADX_MIN) / 12, 0, 1)
    elif adxv >= cfg.ADX_RISING_FLOOR:
        pts += 3
    pts += 3 if sgn * (pdi - ndi) >= cfg.DI_MIN_GAP else 0
    # Volume 15 (4.5 when unavailable: neutral for indices without usable volume)
    vr = vol_ratio(allc, 20)
    if vr is None:
        pts += 4.5
    elif vr >= cfg.VOL_STRONG:
        pts += 15
    elif vr >= cfg.VOL_OK:
        pts += 10.5
    elif vr >= 0.7:
        pts += 4.5
    # ATR 5: volatility comfortably above the floor
    pts += 5 * _clamp(atr_pct / (cfg.MIN_ATR_PCT * 3), 0, 1)

    trig = _trigger(sgn, allc, e9, e21, a)
    detail = {"ema9": e9[-1], "ema21": e21[-1], "vwap": vw[-1], "rsi": r, "adx": adxv, "pdi": pdi, "ndi": ndi,
              "vol_ratio": vr, "atr": a, "atr_pct": atr_pct}
    if not trig:
        return 0.0, "no fresh trigger", detail
    return min(100.0, pts), trig, detail


def _reason(sgn: int, trig: str, d: dict) -> str:
    vr = d.get("vol_ratio")
    vol = f"Vol x{vr:.1f}" if vr else "Vol n/a"
    return (f"{trig}; EMA9 {d['ema9']:.1f} {'>' if sgn > 0 else '<'} EMA21 {d['ema21']:.1f}, "
            f"{'above' if sgn > 0 else 'below'} VWAP {d['vwap']:.1f}, RSI {d['rsi']:.0f}, "
            f"ADX {d['adx']:.0f} (+DI {d['pdi']:.0f}/-DI {d['ndi']:.0f}), "
            f"{vol}, ATR {d['atr']:.1f} ({d['atr_pct']:.2f}%)")


def score_reversal(sgn: int, session: List[Candle], allc: List[Candle]) -> Tuple[float, str, dict]:
    """Counter-trend reversal score for direction `sgn` (+1 = bullish reversal -> CALL). (points, text, detail)."""
    n = cfg.REV_LOOKBACK
    if len(allc) < max(cfg.MIN_BARS, n + 4):
        return 0.0, "not enough bars", {}
    closes = [c.c for c in allc]
    last, prev = allc[-1], allc[-2]
    a = atr(allc, 14)[-1]
    if a <= 0 or last.c <= 0:
        return 0.0, "no ATR", {}
    atr_pct = a / last.c * 100.0
    if atr_pct < cfg.MIN_ATR_PCT:
        return 0.0, f"ATR too small ({atr_pct:.2f}%)", {}
    vr0 = vol_ratio(allc, 20)
    if vr0 is not None and vr0 < cfg.REV_MIN_VOL:
        return 0.0, "reversal without volume", {}                        # exhaustion turns need participation
    e9, e21 = ema(closes, cfg.EMA_FAST), ema(closes, cfg.EMA_SLOW)
    # 1) stretch: price was far beyond EMA21 against the new direction
    dev = [sgn * (allc[-i].c - e21[-i]) / a for i in range(1, n + 1)]
    stretch = -min(dev)
    if stretch < cfg.REV_STRETCH_ATR:
        return 0.0, "no stretch to reverse", {}
    # 2) RSI extreme, now turning
    rv = rsi(closes, 14)
    window = rv[-n:]
    extreme = min(window) if sgn > 0 else max(window)
    if (sgn > 0 and extreme > cfg.REV_RSI_OS) or (sgn < 0 and extreme < cfg.REV_RSI_OB):
        return 0.0, "RSI not at an extreme", {}
    if sgn * (rv[-1] - rv[-2]) <= 0:
        return 0.0, "RSI not turning", {}
    # 4) reversal candle: right colour, strong close, takes out the previous candle's extreme
    cp = close_pos(last)
    strong = cp >= 0.65 if sgn > 0 else cp <= 0.35
    broke = last.c > prev.h if sgn > 0 else last.c < prev.l
    if sgn * (last.c - last.o) <= 0 or not strong or not broke:
        return 0.0, "no reversal candle", {}

    pts = 10 + 5 * min(1.0, (stretch - cfg.REV_STRETCH_ATR) / 1.5)                          # stretch 15
    depth = min(1.0, ((cfg.REV_RSI_OS - extreme) if sgn > 0 else (extreme - cfg.REV_RSI_OB)) / 10.0)
    pts += 15 + 5 * depth                                                                   # RSI 20
    bigger = abs(last.c - last.o) > abs(prev.c - prev.o) and sgn * (prev.c - prev.o) < 0
    pts += 15 + (5 if bigger else 0) * 1 + (0 if body_frac(last) >= 0.5 else -3)             # candle/structure 20
    vr = vol_ratio(allc, 20)
    pts += 7 if vr is None else (15 if vr >= 1.5 else 10 if vr >= 1.2 else 5 if vr >= 1.0 else 0)   # volume 15
    # RSI divergence: price makes a new extreme in the recent half while RSI does not
    h1, h2 = allc[-12:-6], allc[-6:]
    r1, r2 = rv[-12:-6], rv[-6:]
    if len(h1) == 6:
        if sgn > 0 and min(c.l for c in h2) < min(c.l for c in h1) and min(r2) > min(r1):
            pts += 10
        if sgn < 0 and max(c.h for c in h2) > max(c.h for c in h1) and max(r2) < max(r1):
            pts += 10
    pts += 15 if sgn * (last.c - e9[-1]) > 0 else (6 if sgn * (last.c - e9[-1]) > -0.3 * a else 0)   # EMA9 reclaimed
    pts += 5 * min(1.0, atr_pct / (cfg.MIN_ATR_PCT * 3))
    detail = {"rsi": rv[-1], "rsi_extreme": extreme, "stretch": stretch, "vol_ratio": vr,
              "atr": a, "atr_pct": atr_pct, "ema9": e9[-1], "ema21": e21[-1], "vwap": vwap(session)[-1]}
    return min(100.0, pts), "reversal candle", detail


def _rev_reason(sgn: int, d: dict) -> str:
    vr = d.get("vol_ratio")
    return (f"Reversal {'up' if sgn > 0 else 'down'}: price stretched {d['stretch']:.1f} ATR {'below' if sgn > 0 else 'above'} EMA21, "
            f"RSI extreme {d['rsi_extreme']:.0f} now {d['rsi']:.0f} turning, "
            f"reversal candle, {'Vol x%.1f' % vr if vr else 'Vol n/a'}, ATR {d['atr']:.1f}")


def _min_vote_score() -> float:
    # Discovery floor only; final Telegram eligibility is evaluated separately on the 10-point gate.
    return cfg.CANDIDATE_MIN_PRICE_SCORE


STRATEGY_OF = {"PRICE": "TREND", "REVERSAL": "REVERSAL", "ORB": "ORB", "VWAP": "VWAP", "SQUEEZE": "SQUEEZE", "MOMENTUM_EXPLOSION": "MOMENTUM_EXPLOSION", "MOMENTUM_ORB": "ORB", "V7_SEQUENTIAL": "TREND"}


def strategy_of(vote_name: str) -> str:
    return STRATEGY_OF.get(vote_name, "TREND")


def _snapshot(sgn: int, x: Ctx) -> str:
    vr = x.vr
    return (f"EMA9 {x.e9[-1]:.1f} {'>' if x.e9[-1] > x.e21[-1] else '<'} EMA21 {x.e21[-1]:.1f}, "
            f"{'above' if x.last.c > x.vw[-1] else 'below'} VWAP {x.vw[-1]:.1f}, RSI {x.rv[-1]:.0f}, "
            f"ADX {x.adxv:.0f} (+DI {x.pdi:.0f}/-DI {x.ndi:.0f}), "
            f"{'Vol x%.1f' % vr if vr else 'Vol n/a'}, ATR {x.a:.1f} ({x.atr_pct:.2f}%)")


def _quality_adjust(sgn: int, x: Ctx) -> Tuple[float, List[str]]:
    """Points lost for entries that are statistically poor even when the setup is real: chasing a stretched move
    (far from EMA9) and a rejection wick on the signal candle."""
    adj, notes = 0.0, []
    ext = sgn * (x.last.c - x.e9[-1]) / x.a
    if ext > cfg.CHASE_ATR:
        loss = min(12.0, (ext - cfg.CHASE_ATR) * 10.0)
        adj -= loss
        notes.append(f"extended {ext:.1f} ATR from EMA9 (-{loss:.0f})")
    cp = close_pos(x.last)
    if (sgn > 0 and cp < 0.5) or (sgn < 0 and cp > 0.5):
        adj -= 6.0
        notes.append("rejection wick on signal candle (-6)")
    return adj, notes


def all_setups(session: List[Candle], allc: List[Candle]) -> List[Tuple[float, int, str, str]]:
    """Every setup type with points > 0, as (points, sign, vote name, reason). Quality adjustments are applied to the
    non-reversal types; points are NOT yet filtered by the minimum score."""
    out: List[Tuple[float, int, str, str]] = []
    x = Ctx(session, allc)
    for sgn in (1, -1):
        pts, trig, d = score_side(sgn, session, allc)
        if pts > 0:
            out.append((pts, sgn, "PRICE", _reason(sgn, trig, d)))
        if cfg.REVERSAL_ENABLED:
            pts, why, d = score_reversal(sgn, session, allc)
            if pts > 0:
                out.append((pts, sgn, "REVERSAL", _rev_reason(sgn, d)))
        if x.ok():
            for enabled, fn, name in ((cfg.ORB_ENABLED, score_orb, "ORB"), (cfg.VWAP_SETUP_ENABLED, score_vwap, "VWAP"),
                                      (cfg.SQUEEZE_ENABLED, score_squeeze, "SQUEEZE")):
                if not enabled:
                    continue
                pts, trig, d = fn(sgn, x)
                if pts > 0:
                    out.append((pts, sgn, name, f"{trig}; {_snapshot(sgn, x)}"))
            if cfg.MOMENTUM_EXPLOSION_ENABLED:
                pts, trig, d = score_momentum_explosion(sgn, session, allc)
                if pts > 0:
                    out.append((pts, sgn, "MOMENTUM_EXPLOSION", trig))
    adjusted = []
    for pts, sgn, name, reason in out:
        if name != "REVERSAL" and x.ok():
            adj, notes = _quality_adjust(sgn, x)
            pts = max(0.0, pts + adj)
            if notes:
                reason += " | " + "; ".join(notes)
        adjusted.append((pts, sgn, name, reason))
    return adjusted


def price_votes(session: List[Candle], allc: List[Candle], exclude_names=None) -> List[Vote]:
    """At most one vote: the best-scoring setup type. Other types agreeing on the same side add a confluence bonus;
    a strong opposite-side setup makes the picture ambiguous and the symbol is skipped."""
    if len(session) < cfg.MIN_SESSION_BARS or len(allc) < cfg.MIN_BARS:
        return []
    need = _min_vote_score()
    excluded = set(exclude_names or ())
    cands = [c for c in all_setups(session, allc) if c[0] >= need and c[2] not in excluded]
    if not cands:
        return []
    cands.sort(key=lambda c: -c[0])
    top = cands[0]
    opp = [c for c in cands if c[1] == -top[1]]
    if opp and top[0] - opp[0][0] < cfg.CONFLICT_MARGIN:
        return []                                             # conflicting evidence: stay out
    same = [c for c in cands[1:] if c[1] == top[1] and c[2] != top[2]]
    names = sorted({c[2] for c in same})
    pts = min(100.0, top[0] + cfg.CONFLUENCE_BONUS * min(2, len(names)))
    reason = top[3] + (f" | also confirmed by: {', '.join(names)}" if names else "")
    return [Vote(top[2], top[1] * pts / 100.0, reason)]


def price_diagnose(session: List[Candle], allc: List[Candle]) -> str:
    """Why price_votes returned nothing (for the Telegram digest / /status): first failing gate of the better side."""
    if len(session) < cfg.MIN_SESSION_BARS:
        return "few session bars"
    if len(allc) < cfg.MIN_BARS:
        return "warmup (history missing)"
    why = "score below minimum"
    for sgn in (1, -1):
        pts, txt, _ = score_side(sgn, session, allc)
        if pts > 0:
            return "score below minimum"
        if txt and txt not in ("EMA9/21 not up", "EMA9/21 not down"):
            why = txt
    if cfg.REVERSAL_ENABLED and any(score_reversal(sg, session, allc)[0] > 0 for sg in (1, -1)):
        return "score below minimum"
    if any(c[0] > 0 for c in all_setups(session, allc)):
        return "score below minimum"
    return why


def price_score(votes: List[Vote]) -> float:
    if not votes:
        return 0.0
    return _clamp(sum(v.score for v in votes))


def invalidation_distance(side: str, session: List[Candle], allc: List[Candle], spot: float,
                          kind: str = "TREND", trigger_bar: Optional[Candle] = None) -> float:
    """Return the raw underlying distance to a setup-specific invalidation level.

    The stop is placed beyond the structure that created the signal. Distances are
    deliberately NOT widened/clamped here; RiskRules rejects a setup if its true
    structural risk is outside the permitted ATR envelope.
    """
    if not allc or spot <= 0:
        return 0.0
    sgn = 1 if side == "CALL" else -1
    a_values = atr(allc, 14)
    a = float(a_values[-1]) if a_values else 0.0
    if a <= 0:
        return 0.0
    recent = allc[-max(3, min(8, len(allc))):]
    kind = str(kind or "TREND").upper()
    if kind in ("REVERSAL",):
        level = min(c.l for c in recent) if sgn > 0 else max(c.h for c in recent)
    elif kind in ("ORB", "MOMENTUM_ORB", "OPENING_RANGE_BREAKOUT") and len(session) >= cfg.ORB_BARS:
        opening = [c for c in session if (c.ts.hour, c.ts.minute) >= (9, 15)
                   and (c.ts.hour, c.ts.minute) < (9, 30)]
        if len(opening) < cfg.ORB_BARS:
            return 0.0
        level = max(c.h for c in opening[:cfg.ORB_BARS]) if sgn > 0 else min(c.l for c in opening[:cfg.ORB_BARS])
    elif kind in ("MOMENTUM_EXPLOSION", "MEX") and len(allc) > cfg.MEX_LOOKBACK:
        box = allc[-(cfg.MEX_LOOKBACK + 1):-1]
        level = max(c.h for c in box) if sgn > 0 else min(c.l for c in box)
    elif kind == "SQUEEZE" and len(allc) > cfg.SQ_BOX_BARS + 1:
        box = allc[-(cfg.SQ_BOX_BARS + 1):-1]
        level = max(c.h for c in box) if sgn > 0 else min(c.l for c in box)
    elif kind == "VWAP":
        vw_values = vwap(session)
        if not vw_values:
            return 0.0
        level = float(vw_values[-1])
    elif kind in ("SIMPLE_TREND", "NORMAL_TREND"):
        # Include the recent completed 5m swing, but use the nearest valid
        # invalidation reference among swing / EMA9 / session VWAP. This keeps
        # the stop structural without letting a distant swing alone veto a setup.
        swing = allc[-6:-1] if len(allc) >= 6 else allc[:-1]
        if not swing:
            swing = recent
        swing_level = min(float(c.l) for c in swing) if sgn > 0 else max(float(c.h) for c in swing)
        # For a live intrabar entry, include the actual partial trigger-bar
        # extreme as well as the last completed candle's extreme.
        trigger_level = float(
            (trigger_bar.l if sgn > 0 else trigger_bar.h)
            if trigger_bar is not None else
            (allc[-1].l if sgn > 0 else allc[-1].h)
        )
        e9_values = ema([c.c for c in allc], cfg.EMA_FAST)
        vw_values = vwap(session)
        candidates = [float(x) for x in (
            swing_level, trigger_level, e9_values[-1] if e9_values else 0.0,
            vw_values[-1] if vw_values else 0.0,
        ) if x > 0 and (x < spot if sgn > 0 else x > spot)]
        level = (max(candidates) if sgn > 0 else min(candidates)) if candidates else swing_level
    else:
        # Select the nearest valid support/resistance behind the entry, not simply
        # max(EMA21, VWAP), which can select a level on the wrong side of price.
        # Normal trend continuation invalidates at EMA9 (the resumption line),
        # not the much slower EMA21. EMA21 can sit >1.3 ATR away in a healthy trend
        # and would turn otherwise valid entries into impractically wide stops.
        e9_values = ema([c.c for c in allc], cfg.EMA_FAST)
        vw_values = vwap(session)
        # For a continuation entry, the signal candle's own swing is a valid
        # nearby invalidation reference. This avoids rejecting an otherwise clean
        # EMA9 resumption simply because the slower VWAP/EMA9 is temporarily far
        # away. RiskRules still applies the minimum noise buffer and hard max ATR.
        # Use the actual trigger candle extreme for a continuation setup.
        # A 3-8 bar swing can be too distant after a strong trend push and causes
        # good signals to fail the max-risk check. The ATR noise buffer below
        # protects against a stop sitting exactly on the wick.
        trigger = allc[-1]
        swing_level = trigger.l if sgn > 0 else trigger.h
        candidates = [float(x) for x in (e9_values[-1] if e9_values else 0.0,
                                         vw_values[-1] if vw_values else 0.0,
                                         swing_level)
                      if x > 0 and (x < spot if sgn > 0 else x > spot)]
        if candidates:
            level = max(candidates) if sgn > 0 else min(candidates)
        else:
            level = min(c.l for c in recent) if sgn > 0 else max(c.h for c in recent)
    raw = sgn * (spot - float(level)) + cfg.STOP_BUFFER_ATR * a
    return float(raw) if raw > 0 else 0.0


def nearest_structure_obstacle(side: str, allc: List[Candle], spot: float,
                               lookback: int = 24, touch_atr: float = 0.15):
    """Return a nearby, repeatedly tested swing level ahead of price, if present.

    Only completed candles are used. A level needs a confirmed two-bar pivot
    and at least two touches within a small ATR tolerance, reducing noise from
    isolated wicks. CALL looks for resistance; PUT looks for support.
    """
    if len(allc) < 7 or float(spot) <= 0:
        return None
    sgn = 1 if str(side).upper() == "CALL" else -1 if str(side).upper() == "PUT" else 0
    if not sgn:
        return None
    bars = list(allc[-max(7, int(lookback)):])
    av = atr(allc, 14)
    a = float(av[-1]) if av else 0.0
    if a <= 0:
        return None
    tolerance = max(0.01, float(touch_atr) * a)
    pivots = []
    for i in range(2, len(bars) - 2):
        if sgn > 0:
            level = float(bars[i].h)
            pivot = level >= max(float(x.h) for x in bars[i-2:i]) and level >= max(float(x.h) for x in bars[i+1:i+3])
            ahead = level > float(spot)
            touches = sum(1 for x in bars if abs(float(x.h) - level) <= tolerance or
                          float(x.l) - tolerance <= level <= float(x.h) + tolerance)
        else:
            level = float(bars[i].l)
            pivot = level <= min(float(x.l) for x in bars[i-2:i]) and level <= min(float(x.l) for x in bars[i+1:i+3])
            ahead = level < float(spot)
            touches = sum(1 for x in bars if abs(float(x.l) - level) <= tolerance or
                          float(x.l) - tolerance <= level <= float(x.h) + tolerance)
        if pivot and ahead and touches >= 2:
            pivots.append(level)
    if not pivots:
        return None
    return min(pivots) if sgn > 0 else max(pivots)
