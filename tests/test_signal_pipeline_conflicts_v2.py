from datetime import datetime, timedelta, timezone

from signal_engine_v2.config import EngineConfig
from signal_engine_v2.models import (Candidate, Candle, CandleState, MarketSnapshot,
                                     Side, DecisionState)
from signal_engine_v2.pipeline import SignalPipeline


def snapshot(n=220):
    start = datetime(2026, 11, 4, 9, 15, tzinfo=timezone.utc)
    bars = tuple(Candle(start + timedelta(minutes=5*i), 100, 101, 99, 100.5, 1000, True)
                 for i in range(n))
    return MarketSnapshot("NIFTY", "INDEX", "5m", bars[-1], bars,
                          indicators={"adx_14": 25.0, "ema_21": 100.0, "atr_14": 1.0})


def candidate(side, score=80, state=CandleState.CLOSED, strategy="TEST"):
    entry = 100
    stop, target = (95, 110) if side is Side.CALL else (105, 90)
    return Candidate("NIFTY", "INDEX", side, strategy, strategy,
                     snapshot().candle.timestamp, state, score,
                     entry, stop, target)


class Fixed:
    name = "FIXED"
    priority = 1

    def __init__(self, values):
        self.values = values

    def evaluate(self, _):
        return self.values


def pipeline(values, margin=8):
    return SignalPipeline([Fixed(values)], EngineConfig(
        unified_scoring_enabled=False, conflict_score_margin=margin))


def test_equal_opposing_setups_are_rejected_as_conflict():
    snap = snapshot()
    engine = pipeline([candidate(Side.CALL), candidate(Side.PUT)])
    results = engine.evaluate(snap)
    assert len(results) == 2
    assert all(x.state is DecisionState.REJECTED for x in results)
    assert all(x.rejected_reasons == ("directional_conflict_no_clear_winner",) for x in results)


def test_clear_score_winner_survives_opposing_setup():
    snap = snapshot()
    engine = pipeline([candidate(Side.CALL, 92), candidate(Side.PUT, 70)])
    results = engine.evaluate(snap)
    confirmed = [x for x in results if x.state is DecisionState.CONFIRMED]
    assert len(confirmed) == 1
    assert confirmed[0].candidate.side is Side.CALL


def test_closed_bar_beats_higher_scoring_intrabar_preview():
    snap = snapshot()
    values = [candidate(Side.CALL, 70, CandleState.CLOSED),
              candidate(Side.PUT, 99, CandleState.INTRABAR)]
    results = pipeline(values).evaluate(snap)
    confirmed = [x for x in results if x.state is DecisionState.CONFIRMED]
    assert len(confirmed) == 1
    assert confirmed[0].candidate.side is Side.CALL


def test_220_bar_history_is_mandatory():
    short = snapshot(219)
    engine = pipeline([candidate(Side.CALL)])
    results = engine.evaluate(short)
    assert results[0].state is DecisionState.REJECTED
    assert "insufficient_220_bar_history" in results[0].rejected_reasons
