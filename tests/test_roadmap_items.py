"""Audit section B/C items: MEX lifecycle, de-dup, per-strategy floors, performance records, cache re-rank, regime label."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from tests.db_test_utils import make_test_db  # noqa: E402
from engine.analytics import rerank_due, strategy_floor, strategy_performance  # noqa: E402
from models import Candle  # noqa: E402
from utils.market_hours import IST  # noqa: E402


def bar(i, o, h, l, c):
    return Candle(ts=datetime(2026, 10, 15, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i), o=o, h=h, l=l, c=c, v=1000)


class TestStrategyFloor(unittest.TestCase):
    def test_override_can_only_tighten(self):
        self.assertEqual(strategy_floor("REVERSAL", 7.0, "REVERSAL=7.5"), 7.5)
        self.assertEqual(strategy_floor("ORB", 7.0, "ORB=6"), 7.0)            # cannot loosen below the safety floor
        self.assertEqual(strategy_floor("TREND", 7.0, ""), 7.0)
        self.assertEqual(strategy_floor("TREND", 7.0, "garbage,ORB=x"), 7.0)


class TestPerformance(unittest.TestCase):
    def test_closed_outcomes_per_strategy_and_open_not_counted(self):
        sigs = [
            SimpleNamespace(strategy="TREND", result="WIN", pnl_lot=500.0),
            SimpleNamespace(strategy="TREND", result="LOSS", pnl_lot=-300.0),
            SimpleNamespace(strategy="TREND", result="", pnl_lot=0.0),
            SimpleNamespace(strategy="REVERSAL", result="TIME_EXIT", pnl_lot=50.0),
        ]
        out = strategy_performance(sigs)
        self.assertEqual(out["TREND"]["signals"], 3)
        self.assertEqual(out["TREND"]["open"], 1)
        self.assertEqual(out["TREND"]["win_rate"], 0.5)                 # open trade is not a loss
        self.assertEqual(out["REVERSAL"]["win_rate"], None)              # no decided trades
        self.assertEqual(out["TREND"]["pnl_lot"], 200.0)


class TestRerank(unittest.TestCase):
    def test_rerank_due(self):
        self.assertTrue(rerank_due(0, 1000.0, 24))                       # legacy cache without a timestamp
        self.assertFalse(rerank_due(1000.0, 1000.0 + 3600, 24))
        self.assertTrue(rerank_due(1000.0, 1000.0 + 25 * 3600, 24))


class TestRegimeLabel(unittest.TestCase):
    def test_expiry_takes_precedence(self):
        from strategies.regime import regime
        session = [bar(i, 100, 101, 99, 100) for i in range(12)]
        allc = [bar(i, 100, 101, 99, 100) for i in range(40)]
        r = regime(session, 100.0, [], allc, datetime(2026, 10, 15).date(), True)
        self.assertEqual(r["label"], "EXPIRY")


class TestDedupe(unittest.TestCase):
    def test_key_persists_and_purge_drops_old_days(self):
        db = make_test_db()
        today = datetime.now().date()
        old_day = (today - timedelta(days=30)).isoformat()
        recent_day = (today - timedelta(days=2)).isoformat()
        old_key = f"alert:{old_day}:X-1"
        recent_key = f"alert:{recent_day}:X-2"
        db.kv_set(old_key, "1")
        db.kv_set(recent_key, "1")
        db.purge(keep_days=5)
        self.assertIsNone(db.kv_get(old_key))
        self.assertIsNotNone(db.kv_get(recent_key))


if __name__ == "__main__":
    unittest.main()
