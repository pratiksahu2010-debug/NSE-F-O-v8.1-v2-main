"""Regression tests for the independent, simplified strategy engines."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from models import Candle
from strategies.mex_universe import MEX_SYMBOLS
from strategies.orb_universe import ORB_SYMBOLS
from strategies.momentum_explosion import momentum_expansion_index
from strategies.momentum_orb import score_momentum_orb
from strategies.normal_trend import evaluate_normal_trend


IST = ZoneInfo("Asia/Kolkata")


def flat_candles(count=60):
    start = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
    return [Candle(start + timedelta(minutes=5 * i), 100.0, 100.2, 99.8, 100.0, 1000.0, 0.0)
            for i in range(count)]


class TestIndependentStrategyEngines(unittest.TestCase):
    def test_mex_universe_is_fixed_at_100(self):
        self.assertEqual(len(MEX_SYMBOLS), 100)
        self.assertEqual(len(set(MEX_SYMBOLS)), 100)
        self.assertEqual(len(ORB_SYMBOLS), 100)
        self.assertEqual(len(set(ORB_SYMBOLS)), 100)

    def test_normal_engine_does_not_signal_flat_market(self):
        candles = flat_candles()
        result = evaluate_normal_trend(candles[-6:], candles, adx_min=25.0)
        self.assertEqual(result["side"], 0)

    def test_mex_does_not_signal_without_expansion(self):
        candles = flat_candles()
        score, _, _ = momentum_expansion_index(1, candles[-6:], candles)
        self.assertEqual(score, 0.0)

    def test_orb_does_not_signal_without_opening_range_breakout(self):
        candles = flat_candles()
        side, score, _, _ = score_momentum_orb(candles[:8], candles)
        self.assertEqual(side, 0)
        self.assertEqual(score, 0.0)


if __name__ == "__main__":
    unittest.main()
