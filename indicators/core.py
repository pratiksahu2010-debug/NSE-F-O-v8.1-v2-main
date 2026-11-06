"""Pure-python indicators (no pandas) so they are easy to test and cheap to run.

Signal indicators used by the strategy: EMA 9/21, session VWAP, RSI(14), ADX(14) (+DI/-DI), MACD(12,26,9),
relative volume and ATR(14) (volatility floor + stop/target ruler).
"""
from typing import List, Optional, Tuple

from models import Candle


def ema(vals: List[float], n: int) -> List[float]:
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr(candles: List[Candle], n: int = 14) -> List[float]:
    """Wilder ATR aligned to candles.

    Before the first full window, return a running mean for callers that need an
    aligned series. True range restarts at each session open (first bar = high - low), so an overnight gap does not
    inflate intraday ATR, stops and "extension" measurements for the next hour or so. At index n-1, seed Wilder smoothing with the mean of the
    first n true ranges; only then apply the recursive Wilder update. The prior
    implementation recursively smoothed from candle one, biasing ATR toward
    early-session ranges and making the result dependent on how much history
    happened to be loaded.
    """
    if n <= 0:
        raise ValueError("ATR period must be positive")
    if not candles:
        return []
    out: List[float] = []
    trs: List[float] = []
    prev_close = None
    prev_day = None
    current = 0.0
    for i, c in enumerate(candles):
        if c.ts.date() != prev_day:
            prev_close = None          # new session: the overnight gap is not intraday range (see adx())
        prev_day = c.ts.date()
        tr = c.h - c.l if prev_close is None else max(
            c.h - c.l, abs(c.h - prev_close), abs(c.l - prev_close)
        )
        trs.append(max(0.0, tr))
        if len(trs) < n:
            current = sum(trs) / len(trs)
        elif len(trs) == n:
            current = sum(trs) / n
        else:
            current = (current * (n - 1) + trs[-1]) / n
        out.append(current)
        prev_close = c.c
    return out


def has_volume(candles: List[Candle]) -> bool:
    """Index spot candles carry no volume; stock candles do."""
    return any(c.v > 0 for c in candles)


def vwap(session: List[Candle]) -> List[float]:
    """Session VWAP. When the series has no volume (index spot) fall back to the cumulative mean of the
    typical price (TWAP). The old fallback returned each candle's *own* typical price, which made
    "above VWAP" nearly meaningless for indices."""
    out, pv, vv, cum_tp = [], 0.0, 0.0, 0.0
    for i, c in enumerate(session, 1):
        tp = (c.h + c.l + c.c) / 3
        pv += tp * c.v
        vv += c.v
        cum_tp += tp
        out.append(pv / vv if vv > 0 else cum_tp / i)
    return out


def rsi(closes: List[float], n: int = 14) -> List[float]:
    if len(closes) < 2:
        return [50.0] * len(closes)
    gains, losses, out = [], [], [50.0]
    avg_g = avg_l = None
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        gains.append(g)
        losses.append(l)
        if i < n:
            out.append(50.0)
            continue
        if avg_g is None:
            avg_g, avg_l = sum(gains[-n:]) / n, sum(losses[-n:]) / n
        else:
            avg_g = (avg_g * (n - 1) + g) / n
            avg_l = (avg_l * (n - 1) + l) / n
        if avg_l == 0:
            out.append(100.0 if avg_g > 0 else 50.0)       # no losses: 100 only if there were gains; flat = neutral 50
        else:
            out.append(100 - 100 / (1 + avg_g / avg_l))
    return out


def iv_rank(cur: float, hist: List[float]) -> Optional[float]:
    hist = [h for h in hist if h and h > 0]
    if len(hist) < 20 or cur <= 0:
        return None
    lo, hi = min(hist), max(hist)
    return 100.0 * (cur - lo) / (hi - lo) if hi > lo else 50.0


# ---------------------------------------------------------------- trend strength / quality helpers
def adx(candles: List[Candle], n: int = 14) -> List[Tuple[float, float, float]]:
    """Wilder ADX. Returns (adx, +DI, -DI) per candle; zeros until there is enough warm-up (2n+1 candles)."""
    m = len(candles)
    out: List[Tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * m
    if m < 2 * n + 1:
        return out
    tr_l, pdm_l, ndm_l = [], [], []
    for i in range(1, m):
        c, p = candles[i], candles[i - 1]
        if c.ts.date() != p.ts.date():
            # First bar of a new session: the overnight gap is not directional movement inside the session. Counting
            # it made ADX/+DI/-DI read "strong trend" for the first hour of every gap day on pure noise.
            pdm_l.append(0.0)
            ndm_l.append(0.0)
            tr_l.append(max(0.0, c.h - c.l))
            continue
        up, dn = c.h - p.h, p.l - c.l
        pdm_l.append(up if (up > dn and up > 0) else 0.0)
        ndm_l.append(dn if (dn > up and dn > 0) else 0.0)
        tr_l.append(max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c)))

    def di_dx(s_tr, s_p, s_n):
        if s_tr <= 0:
            return 0.0, 0.0, 0.0
        pdi, ndi = 100.0 * s_p / s_tr, 100.0 * s_n / s_tr
        tot = pdi + ndi
        return pdi, ndi, (100.0 * abs(pdi - ndi) / tot if tot else 0.0)

    s_tr, s_p, s_n = sum(tr_l[:n]), sum(pdm_l[:n]), sum(ndm_l[:n])
    pdi, ndi, dx = di_dx(s_tr, s_p, s_n)
    dxs, dis = [dx], [(pdi, ndi)]                      # entry k belongs to candle index n + k
    for j in range(n, len(tr_l)):
        s_tr = s_tr - s_tr / n + tr_l[j]
        s_p = s_p - s_p / n + pdm_l[j]
        s_n = s_n - s_n / n + ndm_l[j]
        pdi, ndi, dx = di_dx(s_tr, s_p, s_n)
        dxs.append(dx)
        dis.append((pdi, ndi))
    a = sum(dxs[:n]) / n
    adxs = {n - 1: a}
    for k in range(n, len(dxs)):
        a = (a * (n - 1) + dxs[k]) / n
        adxs[k] = a
    for k, (pdi, ndi) in enumerate(dis):
        out[n + k] = (adxs.get(k, 0.0), pdi, ndi)
    return out


def with_volume(session: List[Candle], vol_by_ts: dict, min_cover: float = 0.8) -> Tuple[List[Candle], bool]:
    """Return `session` with volume taken from another series (the current-month future for an index).

    Index spot candles carry no volume, so "VWAP" on them is only a time-weighted mean. The future trades the same
    index with real volume, so weighting the *spot* typical price by *futures* volume gives a true VWAP without the
    futures basis (premium) leaking into the spot comparison. If fewer than `min_cover` of the bars have volume we
    return the input unchanged (False) instead of mixing real and missing volume.
    """
    if not session:
        return session, False
    covered = sum(1 for c in session if vol_by_ts.get(c.ts, 0) > 0)
    if covered < min_cover * len(session):
        return session, False
    return [Candle(c.ts, c.o, c.h, c.l, c.c, float(vol_by_ts.get(c.ts, 0.0)), c.oi) for c in session], True


def bars_since_cross(fast: List[float], slow: List[float], max_age: int) -> Optional[Tuple[int, int]]:
    """Most recent fast/slow cross within the last `max_age` bars -> (direction, age) or None.
    direction +1 = fast crossed above slow, -1 = crossed below; age 0 = the latest bar."""
    n = min(len(fast), len(slow))
    for k in range(min(max_age, n - 1)):
        now_d = fast[-1 - k] - slow[-1 - k]
        prev_d = fast[-2 - k] - slow[-2 - k]
        if now_d > 0 >= prev_d:
            return 1, k
        if now_d < 0 <= prev_d:
            return -1, k
    return None


def body_frac(c: Candle) -> float:
    """Body as a share of the candle range (1 = marubozu, 0 = doji)."""
    rng = c.h - c.l
    return abs(c.c - c.o) / rng if rng > 0 else 0.0


def close_pos(c: Candle) -> float:
    """Where the close sits in the candle range: 1 = at the high, 0 = at the low."""
    rng = c.h - c.l
    return (c.c - c.l) / rng if rng > 0 else 0.5


def resample(candles: List[Candle], minutes: int = 15, open_hm=(9, 15)) -> List[Candle]:
    """Aggregate 5m candles into `minutes` bars aligned to the NSE open (09:15). The last bar may be partial."""
    out: List[Candle] = []
    cur, key = None, None
    base = open_hm[0] * 60 + open_hm[1]
    for c in candles:
        k = (c.ts.date(), ((c.ts.hour * 60 + c.ts.minute) - base) // minutes)
        if k != key:
            if cur is not None:
                out.append(cur)
            cur, key = Candle(c.ts, c.o, c.h, c.l, c.c, c.v, c.oi), k
        else:
            cur.h, cur.l, cur.c = max(cur.h, c.h), min(cur.l, c.l), c.c
            cur.v += c.v
            cur.oi = c.oi
    if cur is not None:
        out.append(cur)
    return out


def completed_bars(candles: List[Candle], bars: List[Candle], minutes: int = 15, bar_minutes: int = 5,
                   open_hm=(9, 15)) -> List[Candle]:
    """Return only complete, contiguous HTF buckets; every expected 5m open must exist."""
    if not candles or not bars or minutes <= 0 or bar_minutes <= 0 or minutes % bar_minutes:
        return []
    base = open_hm[0] * 60 + open_hm[1]
    slots = minutes // bar_minutes
    by_day = {}
    for c in candles:
        minute = c.ts.hour * 60 + c.ts.minute
        key = (c.ts.date(), (minute - base) // minutes)
        by_day.setdefault(key, []).append(c.ts.replace(second=0, microsecond=0))
    valid = set()
    for key, stamps in by_day.items():
        day, bucket_no = key
        start_minute = base + bucket_no * minutes
        sample = next(c.ts for c in candles if c.ts.date() == day)
        expected = {sample.replace(hour=(start_minute + i * bar_minutes) // 60,
                                    minute=(start_minute + i * bar_minutes) % 60,
                                    second=0, microsecond=0) for i in range(slots)}
        if expected.issubset(set(stamps)):
            valid.add(key)
    last = max(c.ts for c in candles)
    last_offset = ((last.hour * 60 + last.minute) - base) % minutes
    last_key = (last.date(), ((last.hour * 60 + last.minute) - base) // minutes)
    exclude_last = last_offset != minutes - bar_minutes
    complete = []
    for b in bars:
        minute = b.ts.hour * 60 + b.ts.minute
        key = (b.ts.date(), (minute - base) // minutes)
        if key in valid and not (exclude_last and key == last_key):
            complete.append(b)
    return complete


def htf_bias(candles: List[Candle], minutes: int = 15, fast: int = 9, slow: int = 21,
             completed_only: bool = True) -> int:
    """Higher-timeframe context from resampled candles: +1 up, -1 down, 0 mixed/unknown.

    EMA structure and slope define direction; ADX/DMI confirm whether that direction has
    meaningful trend strength. The caller decides whether neutral is score-only
    context or a hard gate (V3 Normal requires a directional match).
    """
    bars = resample(candles, minutes)
    if completed_only:
        # A half-formed 15m bar repaints on every 5m scan (tiny range, close = last 5m close), which flips the EMA
        # structure / ADX and now matters because 15m opposition is a veto. Use completed bars only.
        bars = completed_bars(candles, bars, minutes)
    if len(bars) < slow + 3:
        return 0
    closes = [b.c for b in bars]
    ef, es = ema(closes, fast), ema(closes, slow)
    up = closes[-1] > es[-1] and ef[-1] > es[-1] and ef[-1] > ef[-3]
    down = closes[-1] < es[-1] and ef[-1] < es[-1] and ef[-1] < ef[-3]
    if not up and not down:
        return 0
    # Weak/flat 15m conditions are treated as neutral, not as an opposing trend.
    # The caller still uses this only as score context, never as a hard alert gate.
    adxv, pdi, ndi = adx(bars, 14)[-1]
    if adxv < 18.0:
        return 0
    if up and pdi > ndi:
        return 1
    if down and ndi > pdi:
        return -1
    return 0


def htf_vwap_bias(session: List[Candle], minutes: int = 15) -> int:
    """Return +1/-1 when the latest *completed* higher-timeframe bar supports
    price being above/below that session's VWAP with VWAP slope confirmation;
    otherwise return 0. This is score context only, never an entry veto.

    The caller supplies today's closed 5-minute session candles so VWAP resets
    at the session open. Since resampling can create a partial final 15m bar,
    discard it unless the last 5m candle is the third candle in its 15m bucket.
    """
    if not session:
        return 0
    bars = resample(session, minutes)
    # Require every expected 5m constituent in each 15m bucket. Merely seeing
    # the third slot as the final timestamp does not detect a missing middle bar.
    bars = completed_bars(session, bars, minutes)
    if not bars:
        return 0
    if len(bars) < 3:
        return 0
    levels = vwap(bars)
    close, level = float(bars[-1].c), float(levels[-1])
    slope = float(levels[-1]) - float(levels[-2])
    if close > level and slope > 0:
        return 1
    if close < level and slope < 0:
        return -1
    return 0


def count_crosses(closes: List[float], levels: List[float]) -> int:
    """How many times price crossed the level series (chop detector for VWAP strategies)."""
    n = 0
    for i in range(1, min(len(closes), len(levels))):
        if (closes[i - 1] - levels[i - 1]) * (closes[i] - levels[i]) < 0:
            n += 1
    return n



def macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Tuple[List[float], List[float], List[float]]:
    """MACD line, signal line, histogram (all aligned to `closes`)."""
    if not closes:
        return [], [], []
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    signal = ema(line, sig)
    return line, signal, [m - s for m, s in zip(line, signal)]


def vol_ratio(candles: List[Candle], n: int = 20) -> Optional[float]:
    """Last candle's volume / mean volume of the previous `n` candles. None when volume is unusable
    (index spot without futures volume, or too little history), so callers can stay neutral."""
    if len(candles) < 6:
        return None
    prev = [c.v for c in candles[-(n + 1):-1] if c.v > 0]
    if len(prev) < 5 or candles[-1].v <= 0:
        return None
    return candles[-1].v / (sum(prev) / len(prev))
