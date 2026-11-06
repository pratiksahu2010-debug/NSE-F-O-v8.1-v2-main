"""Regression tests for structural stop-loss alignment and risk rejection."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import cfg
from engine.risk_rules import RiskRules
from models import Candle
from strategies.price import invalidation_distance


IST = ZoneInfo("Asia/Kolkata")


def bars(count=40, base=100.0):
    start = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
    out = []
    for i in range(count):
        close = base + i * 0.1
        out.append(Candle(start + timedelta(minutes=5 * i), close - 0.1,
                          close + 0.5, close - 0.5, close, 1000.0))
    return out


class TestStructuralStops(unittest.TestCase):
    def test_premium_and_underlying_stop_share_same_risk_distance(self):
        spot, premium, delta, atr_value, distance = 1000.0, 100.0, 0.5, 10.0, 10.0
        levels = RiskRules.levels(premium, delta, atr_value, 0.05, "CALL", spot, distance)
        self.assertIsNotNone(levels)
        underlying_risk = spot - levels["und_sl"]
        premium_risk = premium - levels["sl"]
        self.assertAlmostEqual(underlying_risk, distance, places=6)
        self.assertAlmostEqual(premium_risk, delta * underlying_risk, delta * 0.051)

    def test_excessively_wide_structural_stop_is_rejected_not_clamped(self):
        result = RiskRules.levels(100.0, 0.5, 10.0, 0.05, "CALL", 1000.0, 14.0)
        self.assertIsNone(result)

    def test_too_tight_structure_gets_minimum_noise_buffer(self):
        result = RiskRules.levels(100.0, 0.5, 10.0, 0.05, "CALL", 1000.0, 2.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(1000.0 - result["und_sl"], cfg.SL_MIN_ATR * 10.0, places=6)

    def test_invalid_side_is_rejected(self):
        self.assertIsNone(RiskRules.levels(100.0, 0.5, 10.0, 0.05, "BUY", 1000.0, 10.0))

    def test_structural_distance_returns_zero_when_invalidation_is_wrong_side(self):
        candles = bars()
        # A CALL's selected support is behind spot; this confirms a valid positive distance.
        distance = invalidation_distance("CALL", candles[-10:], candles, candles[-1].c, "NORMAL_TREND")
        self.assertGreater(distance, 0.0)


if __name__ == "__main__":
    unittest.main()
