"""Independent underlying-price setup detectors.

These modules emit candidates only. They do not select option contracts, send
alerts, or place orders. All strategies consume the same precomputed indicator
snapshot and return the same Candidate model.
"""
from __future__ import annotations

from statistics import median
from typing import Optional

from .models import Candidate, CandleState, MarketSnapshot, Side
from .strategy import Strategy


def _bars(snapshot: MarketSnapshot):
    bars = [b for b in snapshot.candles if b.is_closed]
    if snapshot.candle.is_closed and (not bars or bars[-1].timestamp != snapshot.candle.timestamp):
        bars.append(snapshot.candle)
    return bars


def _prior(snapshot: MarketSnapshot):
    return [b for b in _bars(snapshot) if b.timestamp != snapshot.candle.timestamp]


def _ind(snapshot, key, default=None):
    value = snapshot.indicators.get(key, default)
    return float(value) if isinstance(value, (int, float)) else default


def _side_levels(snapshot: MarketSnapshot, side: Side, atr_value: float,
                 structural: Optional[float] = None):
    entry = float(snapshot.candle.close)
    pad = max(atr_value * 0.12, entry * 0.0005)
    if side is Side.CALL:
        stop = min(structural, entry - pad) if structural and structural < entry else entry - max(atr_value, pad)
        risk = entry - stop
        return entry, stop, entry + 1.8 * risk, entry + 2.6 * risk
    stop = max(structural, entry + pad) if structural and structural > entry else entry + max(atr_value, pad)
    risk = stop - entry
    return entry, stop, entry - 1.8 * risk, entry - 2.6 * risk


def _candidate(snapshot, name, priority, side, score, reasons, structural=None, setup_id=None):
    atr_value = _ind(snapshot, "atr_14")
    if not atr_value or atr_value <= 0:
        return None
    entry, stop, t1, t2 = _side_levels(snapshot, side, atr_value, structural)
    return Candidate(
        underlying=snapshot.underlying.upper(),
        instrument_kind=snapshot.instrument_kind.upper(),
        side=side, strategy=name,
        setup_id=setup_id or f"{name}:{snapshot.candle.timestamp.isoformat()}",
        candle_timestamp=snapshot.candle.timestamp,
        candle_state=CandleState.CLOSED if snapshot.candle.is_closed else CandleState.INTRABAR,
        score=max(0.0, min(100.0, float(score))),
        entry=entry, stop=stop, target1=t1, target2=t2,
        reasons=tuple(reasons), strategy_priority=priority,
        htf_confirmed=bool(snapshot.metadata.get("htf_bias") == (1 if side is Side.CALL else -1)),
    )


def _body_fraction(candle):
    width = candle.high - candle.low
    return abs(candle.close - candle.open) / width if width > 0 else 0.0


def _close_position(candle):
    width = candle.high - candle.low
    return (candle.close - candle.low) / width if width > 0 else 0.5


def _volume_bonus(snapshot, threshold=1.25):
    rvol = _ind(snapshot, "rvol_20")
    if rvol is None:
        return 0.0, "volume_unavailable_neutral"
    if rvol < threshold:
        return -12.0, f"relative_volume_below_{threshold:.2f}"
    return min(8.0, (rvol - threshold) * 5.0), f"relative_volume_{rvol:.2f}x"


class BigMomentum(Strategy):
    name, priority = "BIG_MOMENTUM", 10

    def evaluate(self, s):
        i, c = s.indicators, s.candle
        e9, e21, e50 = (_ind(s, "ema_9"), _ind(s, "ema_21"), _ind(s, "ema_50"))
        vwap, adxv = _ind(s, "session_vwap"), _ind(s, "adx_14")
        pdi, ndi, rsi = _ind(s, "plus_di"), _ind(s, "minus_di"), _ind(s, "rsi_14")
        if None in (e9, e21, e50, vwap, adxv, pdi, ndi, rsi):
            return []
        vol_score, vol_reason = _volume_bonus(s, 1.15)
        body = _body_fraction(c)
        pos = _close_position(c)
        hist = _ind(s, "macd_hist")
        out = []
        for side, trend, strength, momentum, location in (
            (Side.CALL, e9 > e21 > e50 and c.close > vwap, pdi > ndi, 52 <= rsi <= 78, pos >= .62),
            (Side.PUT, e9 < e21 < e50 and c.close < vwap, ndi > pdi, 22 <= rsi <= 48, pos <= .38),
        ):
            if trend and strength and momentum and adxv >= 20 and location and body >= .35:
                if hist is not None and ((side is Side.CALL and hist <= 0) or (side is Side.PUT and hist >= 0)):
                    continue
                score = 68 + min(10, (adxv - 20) * .5) + min(8, max(0, vol_score)) + body * 8
                cand = _candidate(s, self.name, self.priority, side, score,
                                  ("ema_9_21_50_aligned", "price_on_correct_vwap_side",
                                   "adx_and_directional_di_confirm", "rsi_momentum_zone", vol_reason))
                if cand: out.append(cand)
        return out


class OpeningRangeBreakout(Strategy):
    name, priority = "ORB", 20

    def evaluate(self, s):
        prior = _prior(s)
        today = [b for b in prior if b.timestamp.date() == s.candle.timestamp.date()]
        # NSE opening range = first three 5m candles. Current candle must break it.
        if len(today) < 3:
            return []
        opening = today[:3]
        hi, lo = max(x.high for x in opening), min(x.low for x in opening)
        if len(today) < 3 or s.candle.timestamp <= opening[-1].timestamp:
            return []
        atrv, adxv = _ind(s, "atr_14"), _ind(s, "adx_14", 0) or 0
        vwap = _ind(s, "session_vwap")
        if not atrv or not vwap:
            return []
        vol_score, vol_reason = _volume_bonus(s, 1.1)
        body = _body_fraction(s.candle)
        out = []
        if s.candle.close > hi and s.candle.close > vwap and body >= .45 and s.candle.close > s.candle.open:
            score = 66 + min(10, max(0, adxv - 15) * .4) + max(0, vol_score) + body * 7
            x = _candidate(s, self.name, self.priority, Side.CALL, score,
                           ("first_15m_range_high_break", "close_above_vwap", vol_reason))
            if x: out.append(x)
        if s.candle.close < lo and s.candle.close < vwap and body >= .45 and s.candle.close < s.candle.open:
            score = 66 + min(10, max(0, adxv - 15) * .4) + max(0, vol_score) + body * 7
            x = _candidate(s, self.name, self.priority, Side.PUT, score,
                           ("first_15m_range_low_break", "close_below_vwap", vol_reason))
            if x: out.append(x)
        return out


class VolumeBreakout(Strategy):
    name, priority = "VOLUME_BREAKOUT", 30

    def evaluate(self, s):
        hi, lo = _ind(s, "donchian_high_20"), _ind(s, "donchian_low_20")
        vwap, e9, e21 = _ind(s, "session_vwap"), _ind(s, "ema_9"), _ind(s, "ema_21")
        if None in (hi, lo, vwap, e9, e21):
            return []
        vol = _ind(s, "rvol_20")
        if vol is None or vol < 1.25:
            return []
        atrv = _ind(s, "atr_14")
        if not atrv:
            return []
        body = _ind(s, "candle_body_fraction", 0) or 0
        out = []
        if s.candle.close > hi and s.candle.close > vwap and e9 > e21 and body >= .4:
            x = _candidate(s, self.name, self.priority, Side.CALL, 73 + min(12, (vol or 1.25) * 3),
                           ("20_bar_donchian_high_break", "ema_9_above_ema_21", "vwap_supports_breakout",
                            "volume_confirmed" if vol is not None else "volume_unavailable_neutral"))
            if x: out.append(x)
        if s.candle.close < lo and s.candle.close < vwap and e9 < e21 and body >= .4:
            x = _candidate(s, self.name, self.priority, Side.PUT, 73 + min(12, (vol or 1.25) * 3),
                           ("20_bar_donchian_low_break", "ema_9_below_ema_21", "vwap_supports_breakdown",
                            "volume_confirmed" if vol is not None else "volume_unavailable_neutral"))
            if x: out.append(x)
        return out


class BreakoutRetest(Strategy):
    name, priority = "BREAKOUT_RETEST", 40

    def evaluate(self, s):
        bars = _prior(s)
        if len(bars) < 22: return []
        atrv, vwap = _ind(s, "atr_14"), _ind(s, "session_vwap")
        e9, e21, rsi = _ind(s, "ema_9"), _ind(s, "ema_21"), _ind(s, "rsi_14")
        if None in (atrv, vwap, e9, e21, rsi): return []
        level_hi, level_lo = max(b.high for b in bars[-21:-1]), min(b.low for b in bars[-21:-1])
        prev = bars[-1]
        vol_score, vol_reason = _volume_bonus(s, 1.0)
        out = []
        if prev.close > level_hi and s.candle.low <= level_hi + .25 * atrv and s.candle.close > level_hi and s.candle.close > vwap and e9 > e21 and rsi > 50:
            x = _candidate(s, self.name, self.priority, Side.CALL, 72 + max(0, vol_score),
                           ("prior_20_bar_high_break", "successful_retest_and_reclaim", vol_reason), structural=level_hi)
            if x: out.append(x)
        if prev.close < level_lo and s.candle.high >= level_lo - .25 * atrv and s.candle.close < level_lo and s.candle.close < vwap and e9 < e21 and rsi < 50:
            x = _candidate(s, self.name, self.priority, Side.PUT, 72 + max(0, vol_score),
                           ("prior_20_bar_low_break", "failed_retest_from_below", vol_reason), structural=level_lo)
            if x: out.append(x)
        return out


class TrendPullback(Strategy):
    name, priority = "TREND_PULLBACK", 50

    def evaluate(self, s):
        bars = _prior(s)
        if len(bars) < 2: return []
        e9, e21, e50 = _ind(s, "ema_9"), _ind(s, "ema_21"), _ind(s, "ema_50")
        vwap, adxv, rsi = _ind(s, "session_vwap"), _ind(s, "adx_14"), _ind(s, "rsi_14")
        atrv = _ind(s, "atr_14")
        if None in (e9, e21, e50, vwap, adxv, rsi, atrv) or atrv <= 0: return []
        c, p = s.candle, bars[-1]
        out = []
        if e9 > e21 > e50 and c.close > vwap and adxv >= 18 and 48 <= rsi <= 68 and c.low <= e9 + .2 * atrv and c.close > e9 and c.close > c.open:
            x = _candidate(s, self.name, self.priority, Side.CALL, 70 + min(10, (adxv - 18) * .5),
                           ("ema_9_21_50_uptrend", "pullback_to_ema_9_zone", "bullish_reclaim_close"))
            if x: out.append(x)
        if e9 < e21 < e50 and c.close < vwap and adxv >= 18 and 32 <= rsi <= 52 and c.high >= e9 - .2 * atrv and c.close < e9 and c.close < c.open:
            x = _candidate(s, self.name, self.priority, Side.PUT, 70 + min(10, (adxv - 18) * .5),
                           ("ema_9_21_50_downtrend", "pullback_to_ema_9_zone", "bearish_rejection_close"))
            if x: out.append(x)
        return out


class VWAPReclaim(Strategy):
    name, priority = "VWAP_RECLAIM_REJECTION", 60

    def evaluate(self, s):
        bars = _prior(s)
        if not bars: return []
        vwap, e9, e21, rsi = (_ind(s, "session_vwap"), _ind(s, "ema_9"),
                               _ind(s, "ema_21"), _ind(s, "rsi_14"))
        atrv = _ind(s, "atr_14")
        if None in (vwap, e9, e21, rsi, atrv) or atrv <= 0: return []
        p, c = bars[-1], s.candle
        slope = _ind(s, "vwap_slope_3", 0) or 0
        out = []
        if p.close <= vwap and c.close > vwap and c.close > c.open and e9 >= e21 and rsi >= 50 and slope > 0:
            x = _candidate(s, self.name, self.priority, Side.CALL, 70,
                           ("previous_close_below_vwap", "current_close_reclaimed_vwap", "positive_vwap_slope"))
            if x: out.append(x)
        if p.close >= vwap and c.close < vwap and c.close < c.open and e9 <= e21 and rsi <= 50 and slope < 0:
            x = _candidate(s, self.name, self.priority, Side.PUT, 70,
                           ("previous_close_above_vwap", "current_close_lost_vwap", "negative_vwap_slope"))
            if x: out.append(x)
        return out


class MomentumReversal(Strategy):
    name, priority = "MOMENTUM_REVERSAL", 70

    def evaluate(self, s):
        bars = _prior(s)
        if len(bars) < 6: return []
        rsi, hist, vwap = _ind(s, "rsi_14"), _ind(s, "macd_hist"), _ind(s, "session_vwap")
        e9, e21, atrv = _ind(s, "ema_9"), _ind(s, "ema_21"), _ind(s, "atr_14")
        if None in (rsi, hist, vwap, e9, e21, atrv) or atrv <= 0: return []
        p, c = bars[-1], s.candle
        # Structure trigger plus momentum turn; RSI/MACD alone never creates a signal.
        prior_low = min(x.low for x in bars[-6:-1])
        prior_high = max(x.high for x in bars[-6:-1])
        out = []
        if c.low < prior_low and c.close > prior_low and c.close > c.open and rsi >= 45 and hist > 0 and e9 > p.close and c.close > vwap:
            x = _candidate(s, self.name, self.priority, Side.CALL, 72,
                           ("swing_low_sweep_and_reclaim", "rsi_recovery", "positive_macd_histogram", "vwap_reclaimed"),
                           structural=c.low)
            if x: out.append(x)
        if c.high > prior_high and c.close < prior_high and c.close < c.open and rsi <= 55 and hist < 0 and e9 < p.close and c.close < vwap:
            x = _candidate(s, self.name, self.priority, Side.PUT, 72,
                           ("swing_high_sweep_and_rejection", "rsi_rollover", "negative_macd_histogram", "vwap_lost"),
                           structural=c.high)
            if x: out.append(x)
        return out


class SqueezeBreakout(Strategy):
    name, priority = "SQUEEZE_BREAKOUT", 80

    def evaluate(self, s):
        bars = _prior(s)
        width, hi, lo, atrv = (_ind(s, "bb_width_pct"), _ind(s, "donchian_high_20"),
                              _ind(s, "donchian_low_20"), _ind(s, "atr_14"))
        e9, e21, vwap = _ind(s, "ema_9"), _ind(s, "ema_21"), _ind(s, "session_vwap")
        if len(bars) < 50 or None in (width, hi, lo, atrv, e9, e21, vwap): return []
        widths = []
        closes = [b.close for b in bars[-50:]]
        # Use rolling widths from historical closes; current width must be near the lower quartile.
        for end in range(20, len(closes) + 1):
            w = closes[end-20:end]
            avg = sum(w) / 20
            if avg:
                sd = (sum((z-avg)**2 for z in w) / 20) ** .5
                widths.append(4 * sd / avg * 100)
        if len(widths) < 20 or width > sorted(widths)[max(0, len(widths)//4)]: return []
        vol, reason = _volume_bonus(s, 1.15)
        out = []
        if s.candle.close > hi and e9 > e21 and s.candle.close > vwap and (vol >= 0):
            x = _candidate(s, self.name, self.priority, Side.CALL, 70 + max(0, vol),
                           ("bollinger_width_compressed", "range_breakout_after_squeeze", reason))
            if x: out.append(x)
        if s.candle.close < lo and e9 < e21 and s.candle.close < vwap and (vol >= 0):
            x = _candidate(s, self.name, self.priority, Side.PUT, 70 + max(0, vol),
                           ("bollinger_width_compressed", "range_breakdown_after_squeeze", reason))
            if x: out.append(x)
        return out


class FailedBreakoutSweep(Strategy):
    name, priority = "FAILED_BREAKOUT_SWEEP", 90

    def evaluate(self, s):
        bars = _prior(s)
        if len(bars) < 21: return []
        atrv, vwap, rsi = _ind(s, "atr_14"), _ind(s, "session_vwap"), _ind(s, "rsi_14")
        if None in (atrv, vwap, rsi) or atrv <= 0: return []
        hi, lo = max(b.high for b in bars[-20:]), min(b.low for b in bars[-20:])
        c = s.candle
        body = _ind(s, "candle_body_fraction", 0) or 0
        close_pos = _close_position(s.candle)
        out = []
        if c.low < lo and c.close > lo and close_pos >= .65 and c.close > c.open and rsi < 65:
            x = _candidate(s, self.name, self.priority, Side.CALL, 74,
                           ("sell_side_liquidity_sweep", "close_reclaimed_prior_swing_low", "bullish_rejection_wick"),
                           structural=c.low)
            if x: out.append(x)
        if c.high > hi and c.close < hi and close_pos <= .35 and c.close < c.open and rsi > 35:
            x = _candidate(s, self.name, self.priority, Side.PUT, 74,
                           ("buy_side_liquidity_sweep", "close_rejected_prior_swing_high", "bearish_rejection_wick"),
                           structural=c.high)
            if x: out.append(x)
        return out


def default_strategies():
    """Return all independent setup detectors in documented priority order."""
    return [BigMomentum(), OpeningRangeBreakout(), VolumeBreakout(), BreakoutRetest(),
            TrendPullback(), VWAPReclaim(), MomentumReversal(), SqueezeBreakout(),
            FailedBreakoutSweep()]
