from datetime import datetime, timedelta, timezone
from dataclasses import replace

from signal_engine_v2 import EngineConfig, SignalPipeline
from signal_engine_v2.models import (Candidate, Candle, CandleState, MarketSnapshot,
                                     Side, DecisionState)


class FixedStrategy:
    def __init__(self, name, candidates, priority=100):
        self.name, self.candidates, self.priority = name, candidates, priority

    def evaluate(self, snapshot):
        return self.candidates


def snapshot(closed=True):
    ts = datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)
    bar = Candle(ts, 100, 105, 99, 104, 1000, closed)
    history_count = 219 if closed else 220
    history = tuple(
        Candle(ts - timedelta(minutes=5 * (history_count - i)),
               100, 101, 99, 100, 1000, True)
        for i in range(history_count)
    )
    bars = history + ((bar,) if closed else ())
    return MarketSnapshot("NIFTY", "INDEX", "5m", bar, bars,
                          indicators={"ema_9": 103.0, "ema_21": 102.0, "ema_50": 101.0,
                                      "session_vwap": 100.0, "vwap_slope_3": 0.2,
                                      "adx_14": 30.0, "plus_di": 30.0, "minus_di": 10.0,
                                      "rsi_14": 60.0, "macd_hist": 0.5, "rvol_20": 2.0,
                                      "atr_14": 2.0},
                          metadata={"htf_bias": 1})

def candidate(strategy="MOMENTUM", side=Side.CALL, score=80, state=CandleState.CLOSED):
    return Candidate("NIFTY", "INDEX", side, strategy, "setup-1", snapshot().candle.timestamp,
                     state, score, 100, 95 if side is Side.CALL else 105,
                     110 if side is Side.CALL else 90, strategy_priority=10)


def test_closed_candidate_is_confirmed():
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate()])])
    decisions = pipe.evaluate(snapshot())
    assert any(x.state is DecisionState.CONFIRMED for x in decisions)


def test_intrabar_candidate_is_explicitly_early():
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate(state=CandleState.INTRABAR)])])
    decisions = pipe.evaluate(snapshot(closed=False))
    assert any(x.state is DecisionState.EARLY for x in decisions)


def test_low_score_is_rejected():
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate(score=64)])],
                          EngineConfig(unified_scoring_enabled=False))
    decisions = pipe.evaluate(snapshot())
    assert any("score_below_minimum" in x.rejected_reasons for x in decisions)


def test_opposite_direction_conflict_without_margin_rejects_both():
    a = candidate("MOMENTUM", Side.CALL, 80)
    b = candidate("BREAKOUT", Side.PUT, 75)
    pipe = SignalPipeline([FixedStrategy("a", [a]), FixedStrategy("b", [b])],
                          EngineConfig(conflict_score_margin=8, unified_scoring_enabled=False))
    decisions = pipe.evaluate(snapshot())
    assert not any(x.state is not DecisionState.REJECTED for x in decisions)
    assert any("directional_conflict_no_clear_winner" in x.rejected_reasons for x in decisions)


def test_repeated_scan_is_deduplicated():
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate()])])
    assert len([x for x in pipe.evaluate(snapshot()) if x.state is DecisionState.CONFIRMED]) == 1
    assert len([x for x in pipe.evaluate(snapshot()) if x.state is DecisionState.CONFIRMED]) == 0

def test_adx_below_22_is_hard_rejected_even_with_high_score():
    snap = snapshot()
    snap = replace(snap, indicators={**snap.indicators, "adx_14": 21.99})
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate(score=95)])],
                          EngineConfig(unified_scoring_enabled=False))
    decisions = pipe.evaluate(snap)
    assert any("adx_below_minimum_22" in x.rejected_reasons for x in decisions)
    assert not any(x.state is not DecisionState.REJECTED for x in decisions)


def test_adx_exactly_22_passes_hard_floor():
    snap = snapshot()
    snap = replace(snap, indicators={**snap.indicators, "adx_14": 22.0})
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate(score=95)])],
                          EngineConfig(unified_scoring_enabled=False))
    decisions = pipe.evaluate(snap)
    assert any(x.state is DecisionState.CONFIRMED for x in decisions)


def test_overextended_entry_is_rejected():
    snap = snapshot()
    snap = replace(snap, indicators={**snap.indicators, "ema_21": 90.0, "atr_14": 2.0})
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate(score=95)])],
                          EngineConfig(unified_scoring_enabled=False, max_entry_extension_atr=1.25))
    decisions = pipe.evaluate(snap)
    assert any("entry_overextended_from_ema21" in x.rejected_reasons for x in decisions)

def test_relative_strength_uses_underlying_minus_index_return():
    from signal_engine_v2.relative_strength import relative_strength
    assert relative_strength(1.8, 0.5) == 1.3
    assert relative_strength(None, 0.5) is None


def test_regime_classification_uses_directional_trend():
    from signal_engine_v2.market_regime import classify_regime
    values = {"adx_14": 27.0, "ema_9": 105.0, "ema_21": 103.0,
              "ema_50": 100.0, "atr_pct": 0.2, "bb_width_pct": 2.0}
    assert classify_regime(values) == "TRENDING_UP"


def test_impulse_detector_labels_multi_factor_expansion():
    from types import SimpleNamespace
    from signal_engine_v2.impulse_detector import detect_impulse
    candle = SimpleNamespace(open=101.0, high=106.0, low=100.0, close=105.0)
    values = {"atr_14": 3.0, "rvol_20": 2.0, "adx_14": 28.0,
              "adx_slope_3": 3.0, "ema_9": 105.0, "ema_21": 103.0,
              "macd_hist": 1.2, "macd_hist_prev": 0.5}
    result = detect_impulse(values, candle)
    assert result["impulse_score"] >= 4.0
    assert result["move_potential"] == "EXPLOSIVE"

def test_engine_config_cannot_lower_adx_floor():
    import pytest
    with pytest.raises(ValueError, match="cannot be lower"):
        EngineConfig(min_adx=21.0)


def test_impulse_direction_does_not_confuse_bearish_expansion_with_bullish():
    from types import SimpleNamespace
    from signal_engine_v2.impulse_detector import detect_impulse

    candle = SimpleNamespace(open=105.0, high=106.0, low=99.0, close=100.0)
    values = {
        "atr_14": 3.0, "rvol_20": 2.0, "adx_14": 28.0,
        "adx_slope_3": 3.0, "ema_9": 101.0, "ema_21": 104.0,
        "macd_hist": -1.2, "macd_hist_prev": -0.5,
        "plus_di": 12.0, "minus_di": 32.0,
    }
    result = detect_impulse(values, candle)
    assert result["impulse_score"] >= 4.0
    assert result["impulse_direction"] == -1
    assert result["directional_impulse_score"] >= 3.0


def test_bullish_impulse_is_not_rewarded_for_put_candidate():
    from signal_engine_v2.scorer import score_candidate

    snap = snapshot()
    snap = replace(snap, metadata={
        **snap.metadata, "impulse_direction": 1,
        "directional_impulse_score": 5.0,
    })
    put = candidate(side=Side.PUT, score=80)
    scored, breakdown = score_candidate(put, snap)
    assert breakdown.setup_quality == 16.0
    assert scored.metadata["impulse_direction"] == 1


def test_deduplication_state_resets_on_new_trading_date():
    pipe = SignalPipeline([FixedStrategy("momentum", [candidate()])])
    first = snapshot()
    assert any(x.state is DecisionState.CONFIRMED for x in pipe.evaluate(first))
    next_day_bar = replace(first.candle, timestamp=first.candle.timestamp + timedelta(days=1))
    next_day_history = tuple(
        replace(bar, timestamp=bar.timestamp + timedelta(days=1))
        for bar in first.candles
    )
    next_day = replace(first, candle=next_day_bar, candles=next_day_history)
    next_day_candidate = replace(candidate(), candle_timestamp=next_day_bar.timestamp)
    pipe.strategies = (FixedStrategy("momentum", [next_day_candidate]),)
    assert any(x.state is DecisionState.CONFIRMED for x in pipe.evaluate(next_day))


def test_rvol_20_requires_all_twenty_reference_bars():
    from signal_engine_v2.indicators import calculate

    ts = datetime(2026, 11, 5, 9, 15, tzinfo=timezone.utc)
    full = tuple(
        Candle(ts + timedelta(minutes=5 * i), 100, 101, 99, 100.5,
               200.0 if i == 21 else 100.0, True)
        for i in range(22)
    )
    result = calculate(full)
    assert result["rvol_20"] == 2.0

    incomplete = list(full)
    incomplete[5] = replace(incomplete[5], volume=0.0)
    result = calculate(tuple(incomplete))
    assert result["rvol_20"] is None
