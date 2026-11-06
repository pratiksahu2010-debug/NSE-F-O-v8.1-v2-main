from datetime import datetime, timedelta, timezone

from signal_engine_v2.config import EngineConfig
from signal_engine_v2.models import (Candidate, Candle, CandleState, MarketSnapshot,
                                     Side, DecisionState)
from signal_engine_v2.pipeline import SignalPipeline
from signal_engine_v2.runtime import evaluate_snapshot


class OneCandidate:
    name = "TEST"
    priority = 1

    def evaluate(self, snapshot):
        return [Candidate("NIFTY", "INDEX", Side.CALL, "TEST", "runtime-1",
                          snapshot.candle.timestamp, CandleState.CLOSED,
                          80, 100, 95, 110)]


def test_runtime_evaluates_candidate_without_portfolio_caps():
    start = datetime(2026, 11, 4, 9, 15, tzinfo=timezone.utc)
    bars = tuple(Candle(start + timedelta(minutes=5*i), 100+i*.01,
                        101+i*.01, 99+i*.01, 100.5+i*.01, 1000, True)
                 for i in range(230))
    bar = bars[-1]
    snap = MarketSnapshot("NIFTY", "INDEX", "5m", bar, bars,
                          indicators={"adx_14": 25.0, "ema_21": bar.close, "atr_14": 1.0})
    engine = SignalPipeline([OneCandidate()], EngineConfig(unified_scoring_enabled=False))
    decisions = evaluate_snapshot(engine, snap)
    assert any(x.state is DecisionState.CONFIRMED for x in decisions)
