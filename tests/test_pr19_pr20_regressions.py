"""Regression coverage for the PR #19 risk rules and PR #20 live-candle fix."""

import unittest
from datetime import datetime

from config import cfg
from data.instruments import Master, build_underlyings
from engine.contract_picker import pick_contract
from engine.risk_rules import RiskRules
from indicators.core import atr
from strategies.price import invalidation_distance
from tests.test_core import TODAY, chain, master_rows, trend_scenario
from utils.market_hours import IST


class TestPr19Pr20Regressions(unittest.TestCase):
    def test_v7_contract_selection_does_not_reject_estimated_spread_cost(self):
        # V7 still requires a valid executable bid/ask, OI, volume and delta;
        # estimated spread percentage is not an additional approval gate.
        master = Master(master_rows())
        underlying = next(u for u in build_underlyings(master, TODAY) if u.name == "NIFTY")
        now = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
        contract, quote, reason = pick_contract(
            master, underlying, "CALL", chain(spread=10.0), now
        )
        self.assertIsNotNone(contract, reason)
        self.assertIsNotNone(quote)

    def test_continuation_trigger_stop_fits_configured_atr_risk_envelope(self):
        history, session = trend_scenario(1, drift=1.5, noise=6.0)
        all_candles = history + session
        spot = session[-1].c
        atr_value = atr(all_candles, 14)[-1]
        distance = invalidation_distance(
            "CALL", session, all_candles, spot, "NORMAL_TREND"
        )
        self.assertGreater(distance, 0.0)
        self.assertLessEqual(distance, cfg.SL_MAX_ATR * atr_value)

        levels = RiskRules.levels(
            120.0, 0.55, atr_value, 0.05, "CALL", spot, stop_dist=distance
        )
        self.assertIsNotNone(levels)
        self.assertAlmostEqual(
            spot - levels["und_sl"], levels["underlying_stop_distance"], places=6
        )


if __name__ == "__main__":
    unittest.main()
