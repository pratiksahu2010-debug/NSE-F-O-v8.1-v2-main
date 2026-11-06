"""Contract ranking on spread/IV (audit B6) and database connection guard (audit B11)."""
import os
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg  # noqa: E402
from db import DB  # noqa: E402
from engine.contract_picker import contract_rank, pick_contract  # noqa: E402
from data.instruments import Master, build_underlyings  # noqa: E402
from models import OptionQuote  # noqa: E402
from tests.test_core import TODAY, chain, master_rows  # noqa: E402
from utils.market_hours import IST  # noqa: E402
from datetime import datetime  # noqa: E402


class TestContractRank(unittest.TestCase):
    def q(self, delta=0.5, bid=99.0, ask=101.0, ltp=100.0, iv=14.0):
        return OptionQuote(25000.0, "CE", ltp=ltp, bid=bid, ask=ask, iv=iv, delta=delta)

    def test_tighter_spread_wins_at_equal_delta(self):
        tight = contract_rank(self.q(bid=99.5, ask=100.5), "CE", True, False, 14.0)
        wide = contract_rank(self.q(bid=90.0, ask=110.0), "CE", True, False, 14.0)
        self.assertLess(tight, wide)

    def test_delta_fit_still_dominates_spread(self):
        near_target = contract_rank(self.q(delta=0.50, bid=95, ask=105), "CE", True, False, 14.0)
        far_from_target = contract_rank(self.q(delta=0.80, bid=99.9, ask=100.1), "CE", False, True, 14.0)
        self.assertLess(near_target, far_from_target)

    def test_iv_dislocation_penalised(self):
        normal = contract_rank(self.q(iv=14.0), "CE", True, False, 14.0)
        dislocated = contract_rank(self.q(iv=40.0), "CE", True, False, 14.0)
        self.assertLess(normal, dislocated)

    def test_picker_still_returns_a_liquid_contract(self):
        m = Master(master_rows())
        u = [x for x in build_underlyings(m, TODAY) if x.name == "NIFTY"][0]
        now = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
        c, q, why = pick_contract(m, u, "CALL", chain(spot=25010.0), now)
        self.assertIsNotNone(c, why)
        self.assertEqual(c.opt_type, "CE")


class TestDbConnectionGuard(unittest.TestCase):
    def test_injected_test_connection_is_closed_cleanly(self):
        conn = sqlite3.connect(":memory:")
        db = DB(connection=conn)
        self.assertEqual(db.backend, "test-injected")
        db.close()
        self.assertIsNone(db.conn)
        with self.assertRaisesRegex(RuntimeError, "connection is closed"):
            db._q("SELECT 1")


if __name__ == "__main__":
    unittest.main()
