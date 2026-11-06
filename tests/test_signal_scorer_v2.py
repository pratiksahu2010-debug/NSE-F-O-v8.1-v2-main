from datetime import datetime, timezone

from signal_engine_v2.models import Candidate, Candle, CandleState, MarketSnapshot, Side
from signal_engine_v2.scorer import score_candidate


def test_unified_scorer_produces_bounded_breakdown():
    bar = Candle(datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc),
                 100, 105, 99, 104, 1000, True)
    snap = MarketSnapshot(
        "NIFTY", "INDEX", "5m", bar,
        indicators={"ema_9": 103, "ema_21": 102, "ema_50": 101,
                    "session_vwap": 100, "vwap_slope_3": .2,
                    "adx_14": 30, "plus_di": 30, "minus_di": 10,
                    "rsi_14": 60, "macd_hist": .5, "rvol_20": 1.8,
                    "atr_14": 2},
        metadata={"htf_bias": 1})
    candidate = Candidate("NIFTY", "INDEX", Side.CALL, "BIG_MOMENTUM", "id",
                          bar.timestamp, CandleState.CLOSED, 75, 104, 100, 111)
    scored, breakdown = score_candidate(candidate, snap)
    assert 0 <= scored.score <= 100
    assert scored.score == breakdown.total
    assert scored.metadata["score_breakdown"]["total"] == scored.score
    assert abs(sum(breakdown.as_dict()[k] for k in breakdown.as_dict() if k != "total") - scored.score) < .01
