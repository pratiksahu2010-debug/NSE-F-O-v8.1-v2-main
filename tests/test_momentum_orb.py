import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

from models import Candle
from strategies import momentum_orb
from strategies.momentum_orb import MomentumORBScanner
from strategies.mex_universe import MEX_SYMBOLS
from utils.market_hours import IST
from config import cfg


class MemoryDB:
    def __init__(self):
        self.values = {}

    def kv_get(self, key):
        return self.values.get(key)

    def kv_set(self, key, value):
        self.values[key] = value


class MomentumORBTests(unittest.TestCase):
    def test_orb_uses_strong_body_close_and_controlled_extension(self):
        start = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        candles = []
        for i in range(39):
            ts = start + timedelta(minutes=5 * i)
            high, low = (102.0, 98.0) if i < 3 else (101.5, 100.5)
            candles.append(Candle(ts, 101.0, high, low, 101.0, 1000))
        last_ts = start + timedelta(minutes=5 * 39)
        candles.append(Candle(last_ts, 102.0, 102.8, 101.9, 102.7, 1800))
        with (
            patch.object(momentum_orb, "atr", return_value=[1.2]),
            patch.object(momentum_orb, "ema", return_value=[102.0]),
            patch.object(cfg, "MOMENTUM_ORB_ENABLED", True),
            patch.object(cfg, "MEX_ORB_BODY_MIN", 0.50),
            patch.object(cfg, "MEX_ORB_CLOSE_POS", 0.65),
            patch.object(cfg, "MEX_ORB_MAX_EXTENSION_ATR", 1.5),
            patch.object(cfg, "MEX_ORB_CLEARANCE_ATR", 0.05),
        ):
            side, score, reason, metrics = momentum_orb.score_momentum_orb(candles, candles)
        self.assertEqual(side, 1, reason)
        self.assertGreater(score, 0)
        self.assertGreaterEqual(metrics["body_fraction"], 0.50)
        self.assertGreaterEqual(metrics["close_position"], 0.65)
        self.assertLessEqual(metrics["extension_atr"], 1.5)
        self.assertAlmostEqual(metrics["orb_midpoint"], (metrics["orb_high"] + metrics["orb_low"]) / 2.0)

    def test_non_mex_symbol_is_not_eligible(self):
        scanner = MomentumORBScanner(MemoryDB())
        result = scanner.scan("NOT_IN_MEX", [], [])
        self.assertFalse(result["eligible"])

    def test_next_bar_retest_confirms_once(self):
        scanner = MomentumORBScanner(MemoryDB())
        start = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        detection = Candle(start + timedelta(minutes=20), 101.1, 102.0, 101.0, 101.8, 2000)
        retest = Candle(start + timedelta(minutes=25), 101.05, 102.1, 100.95, 101.9, 1800)
        metrics = {"orb_high": 101.0, "orb_low": 99.0, "atr": 1.0}
        with (
            patch.object(momentum_orb, "score_momentum_orb", return_value=(1, 82.0, "test ORB breakout", metrics)),
            patch.object(cfg, "MOMENTUM_ORB_ENABLED", True),
            patch.object(cfg, "MEX_ORB_RETEST_ATR", 0.20),
            patch.object(cfg, "MEX_ORB_RETEST_REQUIRED", True),
            patch.object(cfg, "MEX_ORB_SCORE_MIN", 70.0),
        ):
            first = scanner.scan(MEX_SYMBOLS[0], [detection], [detection])
            self.assertEqual(first["state"], "CANDIDATE")
            second = scanner.scan(MEX_SYMBOLS[0], [detection, retest], [detection, retest])
            self.assertTrue(second["confirmed_now"])
            self.assertEqual(second["state"], "CONFIRMED")
            third = scanner.scan(MEX_SYMBOLS[0], [detection, retest], [detection, retest])
            self.assertFalse(third["confirmed_now"])

    def test_breakout_confirms_same_bar_when_retest_is_optional(self):
        scanner = MomentumORBScanner(MemoryDB())
        start = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        detection = Candle(start + timedelta(minutes=20), 101.1, 102.0, 101.0, 101.8, 2000)
        metrics = {"orb_high": 101.0, "orb_low": 99.0, "atr": 1.0}
        with (
            patch.object(momentum_orb, "score_momentum_orb", return_value=(1, 82.0, "test ORB breakout", metrics)),
            patch.object(cfg, "MOMENTUM_ORB_ENABLED", True),
            patch.object(cfg, "MEX_ORB_RETEST_REQUIRED", False),
            patch.object(cfg, "MEX_ORB_SCORE_MIN", 70.0),
        ):
            result = scanner.scan(MEX_SYMBOLS[0], [detection], [detection])
        self.assertTrue(result["confirmed_now"])
        self.assertEqual(result["state"], "CONFIRMED")
        self.assertEqual(result["setup_bar"], detection.ts.isoformat())
        self.assertEqual(result["confirmed_bar"], detection.ts.isoformat())

    def test_orb_no_trigger_reason_is_counted_and_exposed(self):
        scanner = MomentumORBScanner(MemoryDB())
        candle = Candle(datetime(2026, 10, 1, 10, 0, tzinfo=IST), 100, 101, 99, 100, 1000)
        with (
            patch.object(momentum_orb, "score_momentum_orb", return_value=(0, 0.0, "outside ORB entry window", {})),
            patch.object(cfg, "MOMENTUM_ORB_ENABLED", True),
        ):
            scanner.begin_cycle()
            result = scanner.scan(MEX_SYMBOLS[0], [candle], [candle])
        self.assertEqual(result["reason"], "outside ORB entry window")
        snapshot = scanner.snapshot()
        self.assertEqual(snapshot["eligible"], 1)
        self.assertEqual(snapshot["no_trigger"], 1)
        self.assertEqual(snapshot["rejection_reasons"]["outside ORB entry window"], 1)

    def test_gap_after_candidate_expires_instead_of_confirming(self):
        scanner = MomentumORBScanner(MemoryDB())
        start = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        detection = Candle(start + timedelta(minutes=20), 101.1, 102.0, 101.0, 101.8, 2000)
        delayed = Candle(start + timedelta(minutes=30), 101.05, 102.1, 100.95, 101.9, 1800)
        metrics = {"orb_high": 101.0, "orb_low": 99.0, "atr": 1.0}
        with (
            patch.object(momentum_orb, "score_momentum_orb", return_value=(1, 82.0, "test ORB breakout", metrics)),
            patch.object(cfg, "MOMENTUM_ORB_ENABLED", True),
            patch.object(cfg, "MEX_ORB_RETEST_ATR", 0.20),
            patch.object(cfg, "MEX_ORB_RETEST_REQUIRED", True),
            patch.object(cfg, "MEX_ORB_SCORE_MIN", 70.0),
        ):
            self.assertEqual(scanner.scan(MEX_SYMBOLS[0], [detection], [detection])["state"], "CANDIDATE")
            result = scanner.scan(MEX_SYMBOLS[0], [detection, delayed], [detection, delayed])
        self.assertEqual(result["state"], "EXPIRED")
        self.assertFalse(result["confirmed_now"])

    def test_candidate_cannot_confirm_in_next_trading_session(self):
        scanner = MomentumORBScanner(MemoryDB())
        day_one = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        detection = Candle(day_one + timedelta(minutes=20), 101.1, 102.0, 101.0, 101.8, 2000)
        next_session = Candle(datetime(2026, 10, 2, 9, 15, tzinfo=IST), 101.05, 102.1, 100.95, 101.9, 1800)
        metrics = {"orb_high": 101.0, "orb_low": 99.0, "atr": 1.0}
        with (
            patch.object(momentum_orb, "score_momentum_orb", return_value=(1, 82.0, "test ORB breakout", metrics)),
            patch.object(cfg, "MOMENTUM_ORB_ENABLED", True),
            patch.object(cfg, "MEX_ORB_RETEST_ATR", 0.20),
            patch.object(cfg, "MEX_ORB_RETEST_REQUIRED", True),
            patch.object(cfg, "MEX_ORB_SCORE_MIN", 70.0),
        ):
            self.assertEqual(scanner.scan(MEX_SYMBOLS[0], [detection], [detection])["state"], "CANDIDATE")
            result = scanner.scan(MEX_SYMBOLS[0], [next_session], [next_session])
        self.assertEqual(result["state"], "EXPIRED")
        self.assertFalse(result["confirmed_now"])


if __name__ == "__main__":
    unittest.main()
