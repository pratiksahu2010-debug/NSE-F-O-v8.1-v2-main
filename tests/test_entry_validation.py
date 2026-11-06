import unittest
from datetime import datetime, timedelta, timezone

from models import Candle
from engine.entry_validation import EntryCandidate, validate_entry_candidate


class SharedEntryValidationTests(unittest.TestCase):
    def setUp(self):
        self.ts = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
        self.bar = Candle(self.ts, 100, 102, 99, 101, 1000)
        self.candidate = EntryCandidate(
            "TEST", "V7_SEQUENTIAL", "CALL",
            (self.ts - timedelta(minutes=5)).isoformat(),
            self.ts.isoformat(), self.ts.isoformat(), 101.0,
        ).to_dict()

    def check(self, **overrides):
        args = dict(candidate=self.candidate, latest_candle=self.bar,
                    current_price=101.2, atr_value=2.0,
                    max_adverse_atr=0.4, max_runaway_atr=0.6)
        args.update(overrides)
        return validate_entry_candidate(**args)

    def test_fresh_confirmation_passes(self):
        self.assertEqual(self.check()[1], "ENTRY_VALID")

    def test_stale_confirmation_is_rejected(self):
        self.candidate["confirmation_time"] = (self.ts - timedelta(minutes=5)).isoformat()
        self.assertEqual(self.check()[1], "STALE_CONFIRMATION_CANDLE")

    def test_equivalent_timezone_offsets_match(self):
        self.candidate["confirmation_time"] = self.ts.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).isoformat()
        self.bar.ts = self.ts
        self.assertEqual(self.check()[1], "ENTRY_VALID")

    def test_invalid_timestamp_order_is_rejected(self):
        self.candidate["setup_time"] = (self.ts + timedelta(minutes=5)).isoformat()
        self.assertEqual(self.check()[1], "INVALID_ENTRY_TIMESTAMP_ORDER")

    def test_late_entry_is_rejected(self):
        self.assertEqual(self.check(current_price=103.0)[1], "PRICE_EXTENDED_AFTER_CONFIRMATION")

    def test_adverse_move_is_rejected(self):
        self.assertEqual(self.check(current_price=99.5)[1], "ADVERSE_MOVE_AFTER_CONFIRMATION")

    def test_remaining_reward_risk_is_checked(self):
        ok, reason, metrics = self.check(structural_stop=100.0, target=102.0, min_rr=1.2)
        self.assertFalse(ok)
        self.assertEqual(reason, "INSUFFICIENT_REMAINING_REWARD_RISK")
        self.assertLess(metrics["remaining_rr"], 1.2)

    def test_wrong_side_stop_is_rejected(self):
        self.assertEqual(
            self.check(structural_stop=102.0, target=104.0, min_rr=1.2)[1],
            "INVALID_REMAINING_TRADE_STRUCTURE",
        )


if __name__ == "__main__":
    unittest.main()
