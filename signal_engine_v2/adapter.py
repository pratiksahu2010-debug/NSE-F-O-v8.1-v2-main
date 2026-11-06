"""Bridge the existing Upstox/legacy data models into the isolated v2 engine."""
from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional, Sequence

from indicators.core import htf_bias
from models import Candle as LegacyCandle
from .indicators import enrich_snapshot
from .models import Candle, MarketSnapshot


def _convert(bar: LegacyCandle, is_closed: bool = True) -> Candle:
    return Candle(bar.ts, float(bar.o), float(bar.h), float(bar.l),
                  float(bar.c), max(0.0, float(bar.v or 0.0)), is_closed)


def build_snapshot(underlying: str, instrument_kind: str, timeframe: str,
                   candles: Sequence[LegacyCandle],
                   live_candle: Optional[LegacyCandle] = None,
                   futures_volume_by_timestamp: Optional[Mapping] = None,
                   session_candles: Optional[Sequence[LegacyCandle]] = None,
                   metadata: Optional[dict] = None) -> MarketSnapshot:
    """Create a v2 snapshot from existing feed candles.

    Historical candles must be finalized. A supplied live candle remains
    provisional and is never used in indicator warm-up. For index underlyings,
    futures volume can weight the index typical price without introducing
    futures premium/basis into the price series.
    """
    if not candles and live_candle is None:
        raise ValueError("at least one candle is required")
    closed = [_convert(x, True) for x in candles]
    partial = _convert(live_candle, False) if live_candle is not None else None
    current = partial or closed[-1]
    analysis_bars = list(closed)
    details = dict(metadata or {})

    volume_map = futures_volume_by_timestamp or {}
    session_source = [_convert(x, True) for x in (session_candles or candles)
                      if x.ts.date() == current.timestamp.date()]
    if instrument_kind.upper() == "INDEX":
        covered = sum(1 for b in session_source if float(volume_map.get(b.timestamp, 0) or 0) > 0)
        if session_source and volume_map and covered >= 0.8 * len(session_source):
            session_source = [replace(b, volume=max(0.0, float(volume_map.get(b.timestamp, 0) or 0)))
                              for b in session_source]
            # Update matching current-session bars only. Do not invent historical futures volume.
            replacements = {b.timestamp: b.volume for b in session_source}
            analysis_bars = [replace(b, volume=replacements[b.timestamp])
                             if b.timestamp in replacements else b for b in analysis_bars]
            details["volume_source"] = "current_month_future_volume"
        else:
            # Index spot does not trade volume. Never reuse placeholder spot
            # volume when the futures proxy is missing or poorly covered.
            session_source = [replace(b, volume=0.0) for b in session_source]
            session_times = {b.timestamp for b in session_source}
            analysis_bars = [replace(b, volume=0.0) if b.timestamp in session_times else b
                             for b in analysis_bars]
            details["volume_source"] = "unavailable"
    elif instrument_kind.upper() == "STOCK":
        details["volume_source"] = "underlying_equity_volume"
    else:
        details["volume_source"] = "unavailable"

    snap = MarketSnapshot(
        underlying=underlying.upper(),
        instrument_kind=instrument_kind.upper(),
        timeframe=timeframe,
        candle=current,
        candles=tuple(analysis_bars),
        metadata=details,
    )
    snap = enrich_snapshot(snap, session_candles=session_source)
    legacy_closed = [LegacyCandle(b.timestamp, b.open, b.high, b.low, b.close, b.volume, 0)
                     for b in analysis_bars]
    bias = htf_bias(legacy_closed, minutes=15, completed_only=True)
    return replace(snap, metadata={**dict(snap.metadata), "htf_bias": bias})
