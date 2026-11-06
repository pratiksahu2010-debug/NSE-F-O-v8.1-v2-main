"""Signal-quality filters: every test here is a case that used to produce (or could produce) a false signal."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from db import DB  # noqa: E402
from tests.db_test_utils import make_test_db  # noqa: E402
from engine import scorer  # noqa: E402
from unittest.mock import patch  # noqa: E402
from engine.risk_rules import RiskRules  # noqa: E402
from engine.scanner import _closed, fresh_reference  # noqa: E402
from indicators.core import (adx, atr, bars_since_cross, body_frac, close_pos, ema, htf_bias, htf_vwap_bias, resample, rsi, vwap,  # noqa: E402
                             with_volume)
from models import Candle, ChainSnapshot, Underlying  # noqa: E402
from strategies.oi import chain_votes, pcr_shift  # noqa: E402
from strategies import price as price_strategy  # noqa: E402
from strategies.regime import regime  # noqa: E402
from tests.test_core import TODAY, chain, cross_scenario, make_signal, mk_candles, trend_scenario  # noqa: E402
from utils.market_hours import IST  # noqa: E402


class TestIndicators(unittest.TestCase):
    def test_atr_uses_full_window_seed_then_wilder_smoothing(self):
        ts = datetime(2026, 10, 15, 10, 0, tzinfo=IST)
        candles = [
            Candle(ts + timedelta(minutes=5 * i), 100, 100 + r / 2, 100 - r / 2, 100)
            for i, r in enumerate((2, 4, 6))
        ]
        values = atr(candles, 2)
        self.assertEqual(len(values), 3)
        self.assertAlmostEqual(values[0], 2.0)
        self.assertAlmostEqual(values[1], 3.0)   # seed = mean(first two true ranges)
        self.assertAlmostEqual(values[2], 4.5)   # Wilder: (3 * 1 + 6) / 2

    def test_atr_rejects_invalid_period(self):
        with self.assertRaises(ValueError):
            atr([], 0)

    def test_with_volume_gives_index_a_real_vwap(self):
        spot = mk_candles(100, [1, 1, 1, 1, 1], vols=0)
        vol = {c.ts: v for c, v in zip(spot, (1000, 1000, 1000, 1000, 9000))}
        out, ok = with_volume(spot, vol)
        self.assertTrue(ok)
        self.assertEqual([c.v for c in out], [1000, 1000, 1000, 1000, 9000])
        self.assertEqual(spot[0].v, 0)                                   # input is not mutated
        self.assertGreater(vwap(out)[-1], vwap(spot)[-1])                # heavy volume on the last (highest) bar pulls VWAP up
        _, ok = with_volume(spot, {spot[0].ts: 500})                     # 1 of 5 bars covered -> not trusted
        self.assertFalse(ok)

    def test_bars_since_cross(self):
        fast, slow = [1, 1, 2, 3, 4, 5], [2] * 6
        self.assertEqual(bars_since_cross(fast, slow, 3), (1, 2))        # crossed up two bars ago
        self.assertIsNone(bars_since_cross(fast, slow, 2))               # ...too old for a 2-bar window
        self.assertEqual(bars_since_cross([4, 3, 2, 1, 0], [2] * 5, 5), (-1, 1))
        self.assertIsNone(bars_since_cross([3, 3, 3], [2, 2, 2], 3))     # no cross

    def test_vwap_without_volume_is_cumulative_mean(self):
        c = mk_candles(100, [1, 1, -3, 4, 2], vols=0)
        tps = [(x.h + x.l + x.c) / 3 for x in c]
        v = vwap(c)
        self.assertAlmostEqual(v[-1], sum(tps) / len(tps))
        self.assertNotAlmostEqual(v[-1], tps[-1])            # the old fallback returned the candle's own typical price
        self.assertAlmostEqual(v[0], tps[0])

    def test_vwap_with_volume_weights(self):
        c = mk_candles(100, [1, 1], vols=[1000, 3000])
        tps = [(x.h + x.l + x.c) / 3 for x in c]
        self.assertAlmostEqual(vwap(c)[-1], (tps[0] * 1000 + tps[1] * 3000) / 4000)

    def test_adx_separates_trend_from_chop(self):
        trend = mk_candles(100, [3] * 80)
        chop = mk_candles(100, [3, -3] * 40)
        self.assertGreater(adx(trend)[-1][0], 40)
        self.assertGreater(adx(trend)[-1][1], adx(trend)[-1][2])          # +DI > -DI in an uptrend
        self.assertLess(adx(chop)[-1][0], 15)
        self.assertEqual(adx(mk_candles(100, [1] * 10))[-1], (0.0, 0.0, 0.0))   # not enough warm-up

    def test_candle_quality_helpers(self):
        c = Candle(datetime(2026, 10, 15, 10, 0, tzinfo=IST), 100, 110, 100, 109, 1)
        self.assertAlmostEqual(body_frac(c), 0.9)
        self.assertAlmostEqual(close_pos(c), 0.9)
        doji = Candle(c.ts, 100, 110, 90, 100.5, 1)
        self.assertLess(body_frac(doji), 0.05)

    def test_resample_15m_bars_align_to_open(self):
        day = mk_candles(100, [1, -2, 3, 1, 1, 1], day=TODAY)
        bars = resample(day, 15)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].h, max(x.h for x in day[:3]))
        self.assertEqual(bars[0].l, min(x.l for x in day[:3]))
        self.assertEqual(bars[0].c, day[2].c)
        self.assertEqual(bars[0].v, 3000)
        # two sessions never merge into one bar
        two = mk_candles(100, [1] * 3, day=TODAY - timedelta(days=1)) + mk_candles(105, [1] * 3, day=TODAY)
        self.assertEqual(len(resample(two, 15)), 2)

    def test_15m_vwap_bias_uses_completed_bars_and_ignores_partial_last_bar(self):
        rising = mk_candles(100, [1] * 15, vols=[1000] * 15)
        bias_full = htf_vwap_bias(rising)
        bias_partial = htf_vwap_bias(rising[:-1])
        self.assertEqual(bias_full, 1)
        self.assertEqual(bias_partial, bias_full)
        falling = mk_candles(200, [-1] * 15, vols=[1000] * 15)
        self.assertEqual(htf_vwap_bias(falling), -1)
        self.assertEqual(htf_vwap_bias([]), 0)

    def test_htf_bias(self):
        h, s = trend_scenario(1)
        self.assertEqual(htf_bias(h + s), 1)
        h, s = trend_scenario(-1)
        self.assertEqual(htf_bias(h + s), -1)
        self.assertEqual(htf_bias(mk_candles(100, [1] * 10)), 0)          # too little data = unknown


class TestClosedCandles(unittest.TestCase):
    def test_closed_filters_partial_invalid_and_deduplicates(self):
        now = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
        ts = now - timedelta(minutes=10)
        good = Candle(ts, 100, 103, 99, 102, 100)
        duplicate = Candle(ts, 100, 104, 98, 103, 120)
        partial = Candle(now - timedelta(minutes=2), 100, 103, 99, 102, 100)
        malformed = Candle(ts - timedelta(minutes=5), 100, 98, 99, 101, 100)
        out = _closed([good, partial, malformed, duplicate], now)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].h, 104)
        self.assertEqual(out[0].c, 103)






class TestOiFilters(unittest.TestCase):
    def test_small_pcr_jitter_is_ignored(self):
        snap = ChainSnapshot("NIFTY", TODAY, 25000, 1.06)
        self.assertEqual(pcr_shift(snap, 1.0).score, 0)                    # +0.06 used to count as "put writing"
        snap.pcr = 1.15
        self.assertGreater(pcr_shift(snap, 1.0).score, 0)

    def test_stale_oi_reference_is_dropped(self):
        now = datetime(2026, 10, 15, 11, 0, tzinfo=IST)
        mk = lambda dt: {"pcr": 1.0, "call_wall": 25100, "put_wall": 24900, "ts": dt.isoformat(timespec="seconds")}
        self.assertIsNotNone(fresh_reference(mk(now - timedelta(minutes=35)), now))
        self.assertIsNone(fresh_reference(mk(now - timedelta(hours=3)), now))              # hours old
        self.assertIsNone(fresh_reference(mk(now - timedelta(days=1, minutes=35)), now))   # yesterday's EOD snapshot
        self.assertIsNone(fresh_reference(None, now))
        self.assertIsNone(fresh_reference({"ts": "garbage"}, now))


class TestRegime(unittest.TestCase):
    def _reg(self, hhmm, last_close, gap_open=25250.0, prev_close=25000.0):
        h, m = hhmm
        ts = datetime(TODAY.year, TODAY.month, TODAY.day, h, m, tzinfo=IST)
        sess = [Candle(datetime(TODAY.year, TODAY.month, TODAY.day, 9, 15, tzinfo=IST), gap_open, gap_open + 5, gap_open - 5, gap_open, 1),
                Candle(ts, last_close, last_close + 2, last_close - 2, last_close, 1)]
        allc = mk_candles(25000, [1, -1] * 20)
        return regime(sess, prev_close, [], allc, TODAY, False)

    def test_gap_bias_fades_through_the_day(self):
        early, late = self._reg((9, 30), 25260)["score"], self._reg((12, 30), 25260)["score"]
        self.assertGreater(early, 0.3)
        self.assertGreater(early, late)
        self.assertLess(self._reg((14, 0), 25260)["score"], 0.01)

    def test_filled_gap_has_no_bias(self):
        r = self._reg((10, 0), 24990)                       # gapped up 1%, now back under yesterday's close
        self.assertEqual(r["score"], 0.0)
        self.assertTrue(any("filled" in n for n in r["notes"]))


class TestRegimeChop(unittest.TestCase):
    def test_chop_flag_comes_from_adx(self):
        sess = mk_candles(25000, [1, -1] * 3)
        trending = regime(sess, None, [], mk_candles(100, [3] * 80), TODAY, False)
        choppy = regime(sess, None, [], mk_candles(100, [3, -3] * 40), TODAY, False)
        self.assertFalse(trending["chop"])
        self.assertTrue(choppy["chop"])
        self.assertTrue(any("ADX" in n for n in choppy["notes"]))
        self.assertNotIn("er", choppy)                                   # efficiency ratio is gone


class TestRiskRules(unittest.TestCase):
    def setUp(self):
        self.db = make_test_db()
        self.rr = RiskRules(self.db)
        self.now = datetime(2026, 10, 15, 14, 0, tzinfo=IST)
        self.nifty = Underlying("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", lot_size=75)

    def test_structural_stop_rejects_excessive_risk_and_keeps_rr(self):
        a = 40.0
        wide = RiskRules.levels(200.0, 0.55, a, 0.05, "CALL", 25000, stop_dist=5 * a)
        tight = RiskRules.levels(200.0, 0.55, a, 0.05, "CALL", 25000, stop_dist=0.1 * a)
        default = RiskRules.levels(200.0, 0.55, a, 0.05, "CALL", 25000)
        self.assertIsNone(wide)  # do not silently pull a structurally invalid stop closer
        self.assertAlmostEqual(25000 - tight["und_sl"], cfg.SL_MIN_ATR * a)
        self.assertAlmostEqual(25000 - default["und_sl"], cfg.SL_ATR * a)
        for lv in (tight, default):
            self.assertAlmostEqual(200.0 - lv["sl"], 0.55 * (25000 - lv["und_sl"]), delta=0.06)
            self.assertGreaterEqual((lv["und_t1"] - 25000) / (25000 - lv["und_sl"]), cfg.T1_R - 1e-9)
            self.assertGreaterEqual((lv["und_t2"] - 25000) / (25000 - lv["und_sl"]), cfg.T2_R - 1e-9)
        put = RiskRules.levels(200.0, -0.55, a, 0.05, "PUT", 25000, stop_dist=a)
        self.assertGreater(put["und_sl"], 25000)
        self.assertLess(put["und_t1"], 25000)


if __name__ == "__main__":
    unittest.main()


class TestTenPointAlertGate(unittest.TestCase):
    def test_ten_point_score_uses_only_six_requested_components(self):
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        result = scorer.score_10_point("CALL", session, allc, {"chop": False}, htf=1, mkt=1, votes=[object()])
        self.assertEqual(set(result["components"]), {
            "ema_9_21", "vwap", "adx_di", "rsi_momentum", "market_regime", "entry_timing"
        })
        self.assertLessEqual(sum(result["components"].values()), 10.0)
        self.assertGreaterEqual(sum(result["components"].values()), 0.0)
        self.assertEqual(result["metrics"]["score_max"], 10.0)
        self.assertEqual(result["metrics"]["alert_score_minimum"], cfg.ALERT_SCORE_MIN)

    def test_missing_regime_does_not_receive_full_regime_points(self):
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        missing = scorer.score_10_point("CALL", session, allc, None, htf=1, mkt=1, votes=[object()])
        known = scorer.score_10_point("CALL", session, allc, {"chop": False, "score": 0.0}, htf=1, mkt=1, votes=[object()])
        self.assertEqual(missing["components"]["market_regime"], 0.5)
        self.assertGreater(known["components"]["market_regime"], missing["components"]["market_regime"])
        self.assertIn("regime unavailable/invalid", " ".join(missing["reasons"]))
        malformed = scorer.score_10_point("CALL", session, allc, {"chop": False, "score": "bad"}, htf=1, mkt=1, votes=[object()])
        self.assertEqual(malformed["components"]["market_regime"], 0.5)
        self.assertIn("regime unavailable/invalid", " ".join(malformed["reasons"]))

    def test_non_finite_indicator_data_fails_closed(self):
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        with patch("engine.scorer.adx", return_value=[(float("inf"), 35.0, 10.0)]):
            result = scorer.score_10_point("CALL", session, allc, {"chop": False, "score": 0.3}, htf=1, mkt=1, votes=[object()])
        self.assertIn("indicator_data_unavailable", result["rejects"])
        self.assertEqual(result["score"], 0.0)

    def test_adx_24_5_fails_hard_gate_even_if_other_score_is_high(self):
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        with patch("engine.scorer.adx", return_value=[(24.5, 35.0, 10.0)]):
            result = scorer.score_10_point("CALL", session, allc, {"chop": False}, htf=1, mkt=1, votes=[object()])
        self.assertIn("adx_below_25", result["rejects"])
        self.assertEqual(result["components"]["adx_di"], 0.0)

    def test_reversal_uses_its_own_adx_floor_not_trend_adx_25_gate(self):
        from types import SimpleNamespace
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        with patch("engine.scorer.adx", return_value=[(18.0, 12.0, 28.0)]):
            result = scorer.score_10_point(
                "CALL", session, allc, {"chop": False, "score": -0.2},
                htf=-1, mkt=0, votes=[SimpleNamespace(name="REVERSAL")],
            )
        self.assertNotIn("adx_below_25", result["rejects"])
        self.assertNotIn("reversal_adx_below_min", result["rejects"])
        self.assertIn("Reversal ADX context", " ".join(result["reasons"]))

    def test_orb_vwap_squeeze_and_momentum_use_balanced_setup_adx_floor(self):
        from types import SimpleNamespace
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        for setup in ("ORB", "VWAP", "SQUEEZE", "MOMENTUM_EXPLOSION"):
            with self.subTest(setup=setup), patch("engine.scorer.adx", return_value=[(19.9, 30.0, 10.0)]):
                result = scorer.score_10_point(
                    "CALL", session, allc, {"chop": False, "score": 0.2}, htf=1, mkt=1,
                    votes=[SimpleNamespace(name=setup)],
                )
            self.assertIn("setup_adx_below_min", result["rejects"])
            with self.subTest(setup=setup, boundary=True), patch("engine.scorer.adx", return_value=[(20.0, 30.0, 10.0)]):
                boundary = scorer.score_10_point(
                    "CALL", session, allc, {"chop": False, "score": 0.2}, htf=1, mkt=1,
                    votes=[SimpleNamespace(name=setup)],
                )
            self.assertNotIn("setup_adx_below_min", boundary["rejects"])
            self.assertNotIn("adx_below_25", boundary["rejects"])

    def test_trend_setup_still_requires_adx_25(self):
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        with patch("engine.scorer.adx", return_value=[(24.5, 35.0, 10.0)]):
            result = scorer.score_10_point("CALL", session, allc, {"chop": False}, htf=1, mkt=1, votes=[object()])
        self.assertIn("adx_below_25", result["rejects"])

    def test_only_final_score_gate_is_enabled(self):
        self.assertEqual(cfg.CANDIDATE_MIN_PRICE_SCORE, 0.0)
        self.assertEqual(cfg.STAGE1_MIN_SCORE, 0.0)
        self.assertAlmostEqual(price_strategy._min_vote_score(), 0.0)
        self.assertEqual(cfg.ALERT_SCORE_MIN, 8.0)
        self.assertFalse(cfg.EARLY_ENABLED)


class TestSetupAwareGatesRegression(unittest.TestCase):
    """Regression: hard gates must match the setup thesis (reversal vs trend vs trigger)."""

    def _score(self, side, votes, rsi_value):
        from types import SimpleNamespace
        hist, session = trend_scenario(1)
        allc = _closed(hist + session, session[-1].ts + timedelta(minutes=5, seconds=8))
        rsi_series = [float(rsi_value)] * len(allc)
        with patch("engine.scorer.rsi", return_value=rsi_series), \
                patch("engine.scorer.adx", return_value=[(30.0, 20.0, 20.0)]):
            return scorer.score_10_point(side, session, allc, {"chop": False, "score": 0.0}, htf=None, mkt=0,
                                         votes=[SimpleNamespace(name=v) for v in votes])

    def test_reversal_is_not_vetoed_by_trend_side_gates(self):
        # Counter-trend PUT in an uptrend: below-50 RSI, close above VWAP, EMA9>EMA21 are all expected for the
        # reversal thesis and must not hard-reject it (RSI 60 is not oversold, so no reversal hard gate fires).
        result = self._score("PUT", ["REVERSAL"], 60.0)
        for code in ("price_wrong_side_of_vwap", "ema9_21_wrong_side", "rsi_not_bearish", "reversal_adx_below_min"):
            self.assertNotIn(code, result["rejects"])

    def test_trend_put_in_uptrend_is_still_hard_gated(self):
        result = self._score("PUT", ["PRICE"], 60.0)
        self.assertIn("price_wrong_side_of_vwap", result["rejects"])
        self.assertIn("ema9_21_wrong_side", result["rejects"])

    def test_trigger_setup_keeps_vwap_side_but_not_ema_or_rsi50_veto(self):
        result = self._score("PUT", ["ORB"], 60.0)
        self.assertIn("price_wrong_side_of_vwap", result["rejects"])   # VWAP side stays hard
        self.assertNotIn("ema9_21_wrong_side", result["rejects"])      # EMA lag is scored, not vetoed
        self.assertNotIn("rsi_not_bearish", result["rejects"])

    def test_reversal_rsi_extreme_backstop_still_applies(self):
        result = self._score("PUT", ["REVERSAL"], 10.0)
        self.assertIn("rsi_oversold_for_reversal_put", result["rejects"])

