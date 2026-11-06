"""Extra F&O setups built around essential evidence: EMA 9/21, VWAP, RSI, ADX/DI, volume, ATR and candle structure.

Every function returns (points 0-100, short why/trigger text, detail dict); points == 0 means a gate failed (the text
says which). sgn = +1 -> CALL setup, -1 -> PUT setup. Vote names used by price_votes():

  ORB      opening-range breakout with volume, VWAP and candle-quality confirmation
  VWAP     VWAP reclaim/rejection with RSI momentum, volume and fresh-cross confirmation
  SQUEEZE  volatility squeeze breakout with range expansion, volume and ADX/DI confirmation
"""
from typing import List, Optional, Tuple

from config import cfg
from indicators.core import adx, atr, body_frac, close_pos, ema, rsi, vol_ratio, vwap
from models import Candle


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


class Ctx:
    """Indicators computed once per symbol per scan and shared by every setup."""

    def __init__(self, session: List[Candle], allc: List[Candle]):
        self.session, self.allc = session, allc
        self.closes = [c.c for c in allc]
        self.last, self.prev = allc[-1], allc[-2]
        self.atr_s = atr(allc, 14)
        self.a = self.atr_s[-1]
        self.atr_pct = self.a / self.last.c * 100.0 if self.last.c > 0 else 0.0
        self.e9, self.e21 = ema(self.closes, cfg.EMA_FAST), ema(self.closes, cfg.EMA_SLOW)
        self.vw = vwap(session)
        self.rv = rsi(self.closes, 14)
        self.adx_s = adx(allc, 14)
        self.adxv, self.pdi, self.ndi = self.adx_s[-1]
        self.vr = vol_ratio(allc, 20)

    def ok(self) -> bool:
        return self.a > 0 and self.last.c > 0 and self.atr_pct >= cfg.MIN_ATR_PCT


def _vol_pts(vr: Optional[float], top: float) -> float:
    """Volume points (max `top`): strong / ok / weak / unknown (index without futures volume -> ~30%)."""
    if vr is None:
        return 0.3 * top
    if vr >= cfg.VOL_STRONG:
        return top
    if vr >= cfg.VOL_OK:
        return 0.7 * top
    if vr >= 0.7:
        return 0.3 * top
    return 0.0


def _common_gates(sgn: int, x: Ctx) -> Optional[str]:
    """Gates shared by breakout-style setups: right side of VWAP and RSI not exhausted."""
    if sgn * (x.last.c - x.vw[-1]) <= 0:
        return "wrong side of VWAP"
    r = x.rv[-1]
    if (sgn > 0 and r > cfg.RSI_CALL_MAX) or (sgn < 0 and r < cfg.RSI_PUT_MIN):
        return f"RSI exhausted ({r:.0f})"
    return None


# ----------------------------------------------------------------------------------------------------- ORB
def score_orb(sgn: int, x: Ctx) -> Tuple[float, str, dict]:
    s = x.session
    n = cfg.ORB_BARS
    if len(s) < n + 1 or not x.ok():
        return 0.0, "too early for ORB", {}
    t = x.last.ts
    if (t.hour, t.minute) > cfg.ORB_LAST_TIME:
        return 0.0, "ORB window over", {}
    orb = s[:n]
    hi, lo = max(c.h for c in orb), min(c.l for c in orb)
    rng = hi - lo
    if not (cfg.ORB_MIN_RANGE_ATR * x.a <= rng <= cfg.ORB_MAX_RANGE_ATR * x.a):
        return 0.0, "opening range too narrow/wide", {}
    edge = hi if sgn > 0 else lo
    beyond = sgn * (x.last.c - edge)
    if beyond <= 0:
        return 0.0, "inside opening range", {}
    if any(sgn * (c.c - edge) > 0 for c in s[n:-1]):          # a first breakout only: later ones are continuation, not ORB
        return 0.0, "ORB already triggered earlier", {}
    why = _common_gates(sgn, x)
    if why:
        return 0.0, why, {}
    if sgn * (x.last.c - x.e9[-1]) <= 0:
        return 0.0, "below EMA9", {}
    pts = 10 + 10 * _clamp(beyond / (0.5 * x.a))                                   # breakout quality 20
    pts += _vol_pts(x.vr, 20)                                                       # volume 20
    pts += 10 + (5 if len(x.vw) >= 4 and sgn * (x.vw[-1] - x.vw[-4]) > 0 else 0)    # VWAP 15
    pts += 10 if sgn * (x.e9[-1] - x.e21[-1]) > 0 else 5                            # EMA 10
    pts += 10 * _clamp(body_frac(x.last) / 0.7)                                      # candle-body quality 10
    r = x.rv[-1]
    pts += (5 if (cfg.RSI_BULL_MIN <= r if sgn > 0 else r <= cfg.RSI_BEAR_MAX) else 0) + (5 if sgn * (r - x.rv[-2]) > 0 else 0)   # RSI 10
    pts += 10 if x.adxv >= cfg.ADX_MIN else 5 if x.adxv >= cfg.ADX_RISING_FLOOR else 0   # ADX 10
    cp = close_pos(x.last)
    pts += 5 if (cp >= 0.7 if sgn > 0 else cp <= 0.3) else 0                        # candle 5
    d = {"orb_hi": hi, "orb_lo": lo, "adx": x.adxv, "rsi": r, "vol_ratio": x.vr, "atr": x.a}
    return min(100.0, pts), f"opening-range {'high' if sgn > 0 else 'low'} breakout ({edge:.1f})", d


# ----------------------------------------------------------------------------------------------------- VWAP
def score_vwap(sgn: int, x: Ctx) -> Tuple[float, str, dict]:
    s = x.session
    if len(s) < 6 or not x.ok():
        return 0.0, "too early for VWAP", {}
    if sgn * (x.last.c - x.vw[-1]) <= 0:
        return 0.0, "not across VWAP", {}
    k = len(s)
    wrong = [i for i in range(max(0, k - 7), k - 1) if sgn * (s[i].c - x.vw[i]) < 0]
    if len(wrong) < cfg.VWAP_WRONG_BARS or ((k - 2) not in wrong and (k - 3) not in wrong):
        return 0.0, "no fresh VWAP cross", {}
    crossed_by = sgn * (x.last.c - x.vw[-1]) / x.a
    if crossed_by < cfg.VWAP_MIN_CROSS_ATR:
        return 0.0, "VWAP cross too marginal", {}
    r = x.rv[-1]
    if sgn * (r - 50.0) <= 0 or sgn * (r - x.rv[-2]) <= 0:
        return 0.0, "RSI not turning through 50", {}
    if sgn * (x.last.c - x.e9[-1]) <= 0:
        return 0.0, "below EMA9", {}
    if (sgn > 0 and r > cfg.RSI_CALL_MAX) or (sgn < 0 and r < cfg.RSI_PUT_MIN):
        return 0.0, f"RSI exhausted ({r:.0f})", {}
    if x.vr is not None and x.vr < cfg.VOL_OK:
        return 0.0, "VWAP cross without volume expansion", {}          # a cross on thin volume is usually a fake-out
    pts = 8 * _clamp(crossed_by / 0.5) + 5 + 7 * _clamp((len(wrong) - 2) / 3)             # VWAP cross 20
    pts += _vol_pts(x.vr, 20)                                                             # volume 20
    pts += 8 + 12 * _clamp(sgn * (r - 50) / 12)                                           # RSI 20
    pts += 15 + (5 if sgn * (x.e9[-1] - x.e21[-1]) > 0 or sgn * (x.e9[-1] - x.e9[-3]) > 0 else 0)  # EMA 20
    pts += 5 if x.adxv >= cfg.ADX_RISING_FLOOR else 0                                     # ADX 5
    pts += 5 * _clamp(x.atr_pct / (cfg.MIN_ATR_PCT * 3))                                  # ATR 5
    cp = close_pos(x.last)
    pts += 10 if (cp >= 0.6 if sgn > 0 else cp <= 0.4) else 0                             # candle 10
    d = {"vwap": x.vw[-1], "wrong_bars": len(wrong), "adx": x.adxv, "rsi": r, "vol_ratio": x.vr, "atr": x.a}
    return min(100.0, pts), f"VWAP {'reclaim' if sgn > 0 else 'rejection'} after {len(wrong)} candles on the wrong side", d


# --------------------------------------------------------------------------------------------------- SQUEEZE
def score_squeeze(sgn: int, x: Ctx) -> Tuple[float, str, dict]:
    s, allc = x.session, x.allc
    n = cfg.SQ_BOX_BARS
    if len(s) < 4 or len(allc) < n + 30 or not x.ok():
        return 0.0, "not enough bars", {}
    box = allc[-(n + 1):-1]
    bh, bl = max(c.h for c in box), min(c.l for c in box)
    a_prev = x.atr_s[-2]
    peak = max(x.atr_s[-30:-2])
    if a_prev <= 0 or peak <= 0:
        return 0.0, "no ATR", {}
    ratio = a_prev / peak
    if ratio > cfg.SQ_ATR_RATIO or (bh - bl) > cfg.SQ_BOX_ATR * a_prev:
        return 0.0, "no volatility squeeze", {}
    if sgn * (x.last.c - (bh if sgn > 0 else bl)) <= 0:
        return 0.0, "no squeeze breakout", {}
    avg_rng = sum(c.h - c.l for c in box) / n
    exp = (x.last.h - x.last.l) / avg_rng if avg_rng > 0 else 0.0
    if exp < cfg.SQ_EXPANSION:
        return 0.0, "breakout candle not expanding", {}
    adx_prev = x.adx_s[-3][0]
    rising = x.adxv > adx_prev
    di_lead = sgn * (x.pdi - x.ndi) >= 3 * cfg.DI_MIN_GAP      # ADX lags a one-candle break: a clear DI lead also counts
    if not (rising or di_lead):
        return 0.0, "ADX not rising and no DI lead", {}
    why = _common_gates(sgn, x)
    if why:
        return 0.0, why, {}
    if sgn * (x.last.c - x.e21[-1]) <= 0:
        return 0.0, "wrong side of EMA21", {}
    cp = close_pos(x.last)
    pts = 15 * _clamp((cfg.SQ_ATR_RATIO - ratio) / cfg.SQ_ATR_RATIO + 0.4)               # compression 15
    pts += 10 * _clamp((exp - cfg.SQ_EXPANSION) / 1.0) + (10 if (cp >= 0.7 if sgn > 0 else cp <= 0.3) else 4)   # expansion 20
    pts += _vol_pts(x.vr, 20)                                                             # volume 20
    pts += 10 + (5 if sgn * (x.e9[-1] - x.e21[-1]) > 0 else 0)                            # VWAP/EMA 15
    break_distance = sgn * (x.last.c - (bh if sgn > 0 else bl)) / x.a
    pts += 5 * _clamp(break_distance / 0.5) + 5 * _clamp(body_frac(x.last) / 0.7)          # breakout/body quality 10
    r = x.rv[-1]
    pts += (5 if (cfg.RSI_BULL_MIN <= r if sgn > 0 else r <= cfg.RSI_BEAR_MAX) else 0) + (5 if sgn * (r - x.rv[-2]) > 0 else 0)   # RSI 10
    pts += 5 + (5 * _clamp((x.adxv - adx_prev) / 4) if rising else 3)                     # ADX rising / DI lead 10
    d = {"box_hi": bh, "box_lo": bl, "squeeze_ratio": ratio, "expansion": exp, "adx": x.adxv, "rsi": r,
         "vol_ratio": x.vr, "atr": x.a}
    return min(100.0, pts), f"squeeze breakout ({exp:.1f}x range, ATR at {ratio:.0%} of recent peak)", d
