"""Tests for the independent Momentum Explosion detector."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from strategies.momentum_explosion import score_momentum_explosion
from config import cfg
from tests.test_core import trend_scenario
from models import Candle
from utils.market_hours import IST
from datetime import datetime, timedelta


class TestMomentumExplosion(unittest.TestCase):
    def test_insufficient_history_is_rejected_cleanly(self):
        history, session = trend_scenario(1)
        pts, reason, details = score_momentum_explosion(1, session[:4], (history + session)[:12])
        self.assertEqual(pts, 0.0)
        self.assertIn("warmup", reason)
        self.assertEqual(details, {})

    def test_realistic_breakout_is_detected_even_when_rsi_exceeds_normal_entry_band(self):
        # Small, alternating candles establish a modest upward bias. The final candle has
        # a fresh range break, >2x range, 3x volume and strong acceleration without being
        # stretched beyond the MEX chase limit.
        allc, price = [], 25000.0
        start = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
        steps = [0.5 if i % 2 == 0 else -0.25 for i in range(45)]
        for i, step in enumerate(steps):
            o, c = price, price + step
            allc.append(Candle(start + timedelta(minutes=5 * i), o, max(o, c) + 0.7,
                               min(o, c) - 0.7, c, 1000))
            price = c
        allc.append(Candle(start + timedelta(minutes=5 * len(allc)), price,
                           price + 3.3, price - 0.3, price + 3.0, 3000))
        with patch.object(cfg, "MEX_MAX_EXTENSION_ATR", 2.0):
            points, reason, details = score_momentum_explosion(1, allc[-20:], allc)
        self.assertGreater(points, 0.0, reason)
        self.assertIn("MOMENTUM_EXPLOSION UP/CALL", reason)
        self.assertGreater(details["rsi"], 78.0)  # normal entry band must not disable explosion logic
        self.assertGreaterEqual(details["range_ratio"], cfg.MEX_MIN_RANGE_RATIO)

    def test_failed_gates_are_reported_individually(self):
        history, session = trend_scenario(1)
        points, reason, details = score_momentum_explosion(1, session, history + session)
        if points == 0.0 and reason.startswith("MEX blocked:"):
            self.assertTrue(details.get("gate_failures"))
            self.assertEqual(set(details["gate_results"]), {
                "fresh_boundary_break", "strong_close", "not_overextended",
                "minimum_directional_impulse", "minimum_range_expansion",
                "minimum_adx",
            })

    def test_missing_relative_volume_is_diagnostic_not_a_hard_gate(self):
        allc, price = [], 25000.0
        start = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
        for i in range(45):
            step = 0.5 if i % 2 == 0 else -0.25
            o, close = price, price + step
            allc.append(Candle(start + timedelta(minutes=5 * i), o,
                               max(o, close) + 0.7, min(o, close) - 0.7,
                               close, 0))
            price = close
        allc.append(Candle(start + timedelta(minutes=5 * len(allc)), price,
                           price + 3.3, price - 0.3, price + 3.0, 0))
        with patch.object(cfg, "MEX_MAX_EXTENSION_ATR", 2.0):
            points, reason, details = score_momentum_explosion(1, allc[-20:], allc)
        self.assertGreater(points, 0.0, reason)
        self.assertIsNone(details.get("relative_volume"))

    def test_macd_is_supporting_not_a_hard_gate(self):
        allc, price = [], 25000.0
        start = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
        for i in range(45):
            step = 0.5 if i % 2 == 0 else -0.25
            o, close = price, price + step
            allc.append(Candle(start + timedelta(minutes=5 * i), o,
                               max(o, close) + 0.7, min(o, close) - 0.7,
                               close, 1000))
            price = close
        allc.append(Candle(start + timedelta(minutes=5 * len(allc)), price,
                           price + 3.3, price - 0.3, price + 3.0, 3000))
        # Deliberately bearish/weak MACD while price impulse and expansion are bullish.
        with patch.object(cfg, "MEX_MAX_EXTENSION_ATR", 2.0), \
             patch("strategies.momentum_explosion.macd", return_value=([], [], [-1.0, -2.0])):
            points, reason, details = score_momentum_explosion(1, allc[-20:], allc)
        self.assertGreater(points, 0.0, reason)
        self.assertFalse(details["macd_supporting"])
        self.assertNotIn("macd", details["gate_results"])

    def test_both_directions_are_evaluated_without_crashing(self):
        for sign in (1, -1):
            history, session = trend_scenario(sign)
            pts, reason, details = score_momentum_explosion(sign, session, history + session)
            self.assertGreaterEqual(pts, 0.0)
            self.assertLessEqual(pts, 100.0)
            self.assertIsInstance(reason, str)
            self.assertIsInstance(details, dict)


if __name__ == "__main__":
    unittest.main()
