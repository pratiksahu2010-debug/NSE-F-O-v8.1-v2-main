from datetime import datetime, timedelta, timezone

from signal_engine_v2.models import Candle, MarketSnapshot
from signal_engine_v2.strategies import default_strategies


def test_all_nine_independent_strategies_are_registered():
    strategies = default_strategies()
    assert len(strategies) == 9
    assert len({s.name for s in strategies}) == 9
    assert [s.name for s in strategies] == [
        "BIG_MOMENTUM", "ORB", "VOLUME_BREAKOUT", "BREAKOUT_RETEST",
        "TREND_PULLBACK", "VWAP_RECLAIM_REJECTION", "MOMENTUM_REVERSAL",
        "SQUEEZE_BREAKOUT", "FAILED_BREAKOUT_SWEEP"
    ]


def test_strategies_return_candidates_or_empty_without_crashing_on_warmup():
    start = datetime(2026, 11, 5, 9, 15, tzinfo=timezone.utc)
    bars = tuple(Candle(start + timedelta(minutes=5*i), 100, 101, 99, 100, 1000, True)
                 for i in range(10))
    snap = MarketSnapshot("NIFTY", "INDEX", "5m", bars[-1], bars,
                          indicators={"atr_14": 1.0, "ema_9": 100.0,
                                      "ema_21": 100.0, "ema_50": 100.0,
                                      "session_vwap": 100.0, "adx_14": 10.0,
                                      "plus_di": 10.0, "minus_di": 10.0,
                                      "rsi_14": 50.0, "rvol_20": None,
                                      "macd_hist": 0.0, "vwap_slope_3": 0.0})
    for strategy in default_strategies():
        result = strategy.evaluate(snap)
        assert isinstance(result, list)
