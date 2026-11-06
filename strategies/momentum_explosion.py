"""Independent Momentum Expansion Index (MEI) detector for the MEX engine.

The detector seeks early compression release or strong range expansion with an
ATR-normalised directional impulse, a fresh price-boundary break, strong close and an EMA9 extension guard. MACD histogram, relative volume and
RSI are supporting diagnostics only; VWAP/ADX alignment and second-candle
lifecycle confirmation are not required.
"""
from statistics import median
from typing import List, Tuple
from config import cfg
from indicators.core import adx, atr, close_pos, ema, macd, rsi
from models import Candle


def _clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))


def momentum_expansion_index(sgn: int, session: List[Candle], allc: List[Candle]) -> Tuple[float, str, dict]:
    """Early compression-release detector; +1 is CALL, -1 is PUT.

    The detector looks for a volatility contraction followed by a directional
    range break with a strong directional impulse. MACD and volume are diagnostic,
    never hard gates. It deliberately does not wait for a second candle.
    """
    if sgn not in (1, -1) or len(allc) < 35 or len(session) < 3:
        return 0.0, "MEX warmup", {}
    current, prior = allc[-1], allc[-2]
    av = atr(allc, 14)[-1]
    if not av or av <= 0:
        return 0.0, "MEX ATR unavailable", {}
    closes = [x.c for x in allc]
    rsi_value = rsi(closes, 14)[-1]
    adx_value, plus_di, minus_di = adx(allc, 14)[-1]
    directional_di = plus_di > minus_di if sgn > 0 else minus_di > plus_di
    e9 = ema(closes, 9)[-1]
    ml, ms, hist = macd(closes, 12, 26, 9)
    lookback = max(5, int(cfg.MEX_LOOKBACK))
    base = allc[-(lookback + 1):-1]
    if len(base) < lookback:
        return 0.0, "MEX range baseline unavailable", {}
    base_ranges = [max(0.0, x.h - x.l) for x in base]
    recent_compression = median(base_ranges)
    broad = [max(0.0, x.h - x.l) for x in allc[-31:-1]]
    broad_median = median(broad) if broad else recent_compression
    compression_ratio = recent_compression / max(broad_median, 1e-12)
    current_range = max(0.0, current.h - current.l)
    range_ratio = current_range / max(recent_compression, 1e-12)
    impulse = sgn * (current.c - current.o) / av
    macd_turn = hist[-1] > hist[-2] if sgn > 0 else hist[-1] < hist[-2]
    macd_direction = hist[-1] > 0 if sgn > 0 else hist[-1] < 0
    macd_support = macd_turn or macd_direction
    boundary = max(x.h for x in base) if sgn > 0 else min(x.l for x in base)
    breakout = sgn * (current.c - boundary) > 0
    close_position = close_pos(current)
    close_threshold = float(getattr(cfg, "MEX_CLOSE_POS", 0.75))
    strong_close = close_position >= close_threshold if sgn > 0 else close_position <= (1.0 - close_threshold)
    extension = sgn * (current.c - e9) / av
    not_extended = extension <= float(cfg.MEX_MAX_EXTENSION_ATR)
    volume_values = [x.v for x in allc[-21:-1] if x.v > 0]
    rvol = current.v / (sum(volume_values) / len(volume_values)) if current.v > 0 and volume_values else None
    compressed = compression_ratio <= float(cfg.MEX_COMPRESSION_MAX)
    expansion = range_ratio >= float(cfg.MEX_MIN_RANGE_RATIO)
    # Compression is recorded as context, but the current trigger candle must
    # independently meet the configured range-expansion threshold.
    gates = {
        "minimum_adx": float(adx_value) >= 18.0,
        "fresh_boundary_break": breakout,
        "strong_close": strong_close,
        "not_overextended": not_extended,
        "minimum_directional_impulse": impulse >= float(cfg.MEX_MIN_IMPULSE_ATR),
        "minimum_range_expansion": expansion,
    }
    failed = [name for name, passed in gates.items() if not passed]
    diagnostics = {
        "compression_ratio": compression_ratio, "range_ratio": range_ratio,
        "adx": float(adx_value), "plus_di": float(plus_di), "minus_di": float(minus_di),
        "impulse_atr": impulse, "macd_histogram": hist[-1],
        "macd_turn": macd_turn, "macd_direction_aligned": macd_direction,
        "macd_supporting": macd_support, "extension_atr": extension, "relative_volume": rvol,
        "gate_results": gates, "gate_failures": failed,
        "breakout_level": boundary, "close_position": close_position,
    }
    if failed:
        return 0.0, "MEX blocked: " + ", ".join(failed), diagnostics
    score = min(100.0, 55.0 + min(20.0, max(0.0, impulse) * 12.0)
                + min(15.0, max(0.0, range_ratio - 1.0) * 8.0)
                + (10.0 if compressed else 0.0))
    details = {"mei": round(score, 2), "range_ratio": range_ratio,
               "relative_volume": rvol, "impulse_atr": impulse,
               "compression_ratio": compression_ratio, "compressed_before_release": compressed,
               "macd_histogram": hist[-1], "macd_previous": hist[-2],
               "adx": float(adx_value), "plus_di": float(plus_di), "minus_di": float(minus_di),
               "rsi": float(rsi_value), "ema9": e9, "extension_atr": extension,
               "close_position": close_position, "breakout_level": boundary,
               "macd_turn": macd_turn, "macd_direction_aligned": macd_direction,
               "macd_supporting": macd_support, "gate_results": gates, "gate_failures": [], "atr": av}
    direction = "UP/CALL" if sgn > 0 else "DOWN/PUT"
    return score, f"MOMENTUM_EXPLOSION {direction}: ATR impulse, range expansion and fresh boundary break", details


def score_momentum_explosion(sgn: int, session: List[Candle], allc: List[Candle]):
    """Compatibility wrapper for the existing strategy registry."""
    return momentum_expansion_index(sgn, session, allc)
