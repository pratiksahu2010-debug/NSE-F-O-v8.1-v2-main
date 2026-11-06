"""Essential-indicator strategy (EMA 9/21, VWAP, RSI, ADX, volume, ATR), scorer, /ping and no-order guarantee."""
import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from engine import scorer  # noqa: E402
from engine.risk_rules import RiskRules  # noqa: E402
from indicators.core import macd, vol_ratio  # noqa: E402
from models import Candle  # noqa: E402
from strategies.price import invalidation_distance, price_diagnose, price_votes, score_side  # noqa: E402
from tests.test_core import mk_candles, trend_scenario  # noqa: E402


class TestIndicators(unittest.TestCase):
    def test_macd_histogram_follows_trend(self):
        up = macd([100 + i for i in range(60)])
        self.assertGreater(up[2][-1] + up[0][-1], 0)
        self.assertGreater(up[0][-1], 0)
        down = macd([200 - i for i in range(60)])
        self.assertLess(down[0][-1], 0)

    def test_vol_ratio(self):
        c = mk_candles(100, [0.1] * 25, [100] * 24 + [250])
        self.assertAlmostEqual(vol_ratio(c), 2.5)
        self.assertIsNone(vol_ratio(mk_candles(100, [0.1] * 25, 0)))     # index spot: no volume -> neutral


class TestPriceScore(unittest.TestCase):
    def setUp(self):
        self.hist, self.sess = trend_scenario(1)
        self.allc = self.hist + self.sess

    def test_clean_trend_scores_high_both_sides(self):
        for sign in (1, -1):
            h, s = trend_scenario(sign)
            pts, trig, _ = score_side(sign, s, h + s)
            self.assertGreaterEqual(pts, cfg.PRICE_MIN_SCORE, (sign, trig))

    def test_wrong_side_returns_gate_reason(self):
        pts, why, _ = score_side(-1, self.sess, self.allc)
        self.assertEqual(pts, 0.0)
        self.assertIn("EMA9/21", why)

    def test_low_volatility_blocked_by_atr_floor(self):
        h, s = trend_scenario(1, scale=0.5)
        self.assertEqual(price_votes(s, h + s), [])
        self.assertIn("ATR", price_diagnose(s, h + s))

    def test_volume_lifts_score(self):
        base = score_side(1, self.sess, self.allc)[0]
        s2 = list(self.sess)
        s2[-1] = replace(s2[-1], v=5000.0)
        boosted = score_side(1, s2, self.hist + s2)[0]
        self.assertGreater(boosted, base)

    def test_no_history_is_reported_as_warmup(self):
        self.assertEqual(price_votes(self.sess, self.sess), [])
        self.assertIn("warmup", price_diagnose(self.sess, self.sess))

    def test_exhausted_rsi_blocked(self):
        old = cfg.RSI_CALL_MAX
        try:
            cfg.RSI_CALL_MAX = 30.0
            self.assertEqual(score_side(1, self.sess, self.allc)[0], 0.0)
        finally:
            cfg.RSI_CALL_MAX = old

    def test_structural_stop_behind_ema21_vwap(self):
        d = invalidation_distance("CALL", self.sess, self.allc, self.allc[-1].c)
        self.assertGreater(d, 0)


class TestScorerIndependentConfirmation(unittest.TestCase):
    def test_price_alone_can_pass_without_oi_or_chain_confirmation(self):
        r, why = scorer.evaluate({"price": 0.85, "oi": 0.0, "chain": 0.0, "regime": 0.0})
        self.assertIsNotNone(r, why)
        self.assertEqual(r[0], "CALL")

    def test_oi_is_not_required_for_confirmation(self):
        r, why = scorer.evaluate({"price": 0.8, "oi": 0.6, "chain": 0.0, "regime": 0.0})
        self.assertIsNotNone(r, why)
        self.assertEqual(r[0], "CALL")
        self.assertEqual(r[2], 1)

    def test_oi_does_not_change_score_or_veto(self):
        base = scorer.evaluate({"price": 0.85, "oi": 0.2})[0][1]
        self.assertEqual(scorer.evaluate({"price": 0.85, "oi": 0.9})[0][1], base)
        self.assertIsNotNone(scorer.evaluate({"price": 0.85, "oi": -0.9})[0])

    def test_weak_price_fails_the_single_final_score_gate(self):
        self.assertIsNone(scorer.evaluate({"price": 0.4, "oi": 0.9, "chain": 0.9})[0])

    def test_15m_against_is_a_validation_point_not_a_veto(self):
        # The 15m trend stays a scored validation point: against it lowers the score, it does not hard-reject.
        with_htf = scorer.evaluate({"price": 0.95, "oi": 0.5}, htf=-1)[0]
        aligned = scorer.evaluate({"price": 0.90, "oi": 0.5}, htf=1)[0]
        self.assertIsNotNone(with_htf)
        self.assertLess(with_htf[1], aligned[1])


    def test_15m_vwap_is_small_score_context_not_a_hard_gate(self):
        h, sess = trend_scenario(1)
        allc = h + sess
        base = scorer.score_10_point("CALL", sess, allc, {"chop": False, "score": 0.0}, htf=1, votes=[], htf_vwap=0)
        opposed = scorer.score_10_point("CALL", sess, allc, {"chop": False, "score": 0.0}, htf=1, votes=[], htf_vwap=-1)
        self.assertAlmostEqual(base["score"] - opposed["score"], 0.15, places=2)
        self.assertEqual(opposed["rejects"], base["rejects"])
        self.assertTrue(any("not a veto" in r for r in opposed["reasons"]))



class TestChopGate(unittest.TestCase):
    def test_choppy_regime_is_a_hard_reject_without_breakout_expansion(self):
        h, sess = trend_scenario(1)
        result = scorer.score_10_point("CALL", sess, h + sess, regime={"chop": True, "score": 0.0}, votes=[])
        self.assertIn("market_chop_without_expansion_breakout", result["rejects"])

    def test_chop_gate_can_be_disabled_explicitly_for_diagnostics(self):
        h, sess = trend_scenario(1)
        old = cfg.CHOP_GATE_ENABLED
        try:
            cfg.CHOP_GATE_ENABLED = False
            result = scorer.score_10_point("CALL", sess, h + sess, regime={"chop": True, "score": 0.0}, votes=[])
            self.assertNotIn("market_chop_without_expansion_breakout", result["rejects"])
        finally:
            cfg.CHOP_GATE_ENABLED = old

class TestNoRiskBudgetRule(unittest.TestCase):
    def test_config_and_rules_have_no_per_trade_risk_cap(self):
        for name in ("RISK_CAPITAL", "RISK_PER_TRADE_PCT", "DAILY_LOSS_LIMIT_PCT", "MAX_TOTAL_OPEN_RISK_PCT"):
            self.assertFalse(hasattr(cfg, name), name)
        self.assertFalse(hasattr(RiskRules, "sizing"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- reversal + early tier
import random  # noqa: E402
from datetime import timedelta  # noqa: E402

from strategies.price import score_reversal  # noqa: E402
from tests.test_core import TODAY  # noqa: E402


def reversal_scenario(sign=1, scale=3.0, vol_last=3000):
    """Orderly drift against `sign`, a sharp flush that stretches price far beyond EMA21, then a big reversal candle.
    sign=+1 -> bullish reversal (CALL); sign=-1 is the exact mirror."""
    rng = random.Random(5)
    steps = [rng.gauss(-0.4 * scale, 3 * scale) for _ in range(150)]
    h1 = mk_candles(25000, [sign * x for x in steps[:75]], day=TODAY - timedelta(days=2))
    h2 = mk_candles(h1[-1].c, [sign * x for x in steps[75:]], day=TODAY - timedelta(days=1))
    day = [-3, -4, -5, -6, -7, -6, -5, -3, -2, 9]
    vols = [1000] * 9 + [vol_last]
    sess = mk_candles(h2[-1].c, [sign * x * scale for x in day], vols)
    return h1 + h2, sess


class TestReversal(unittest.TestCase):
    def test_reversal_fires_both_directions_when_explicitly_enabled(self):
        old = cfg.REVERSAL_ENABLED
        try:
            cfg.REVERSAL_ENABLED = True
            for sign in (1, -1):
                h, s = reversal_scenario(sign)
                pts, why, _ = score_reversal(sign, s, h + s)
                self.assertGreaterEqual(pts, cfg.PRICE_MIN_SCORE, (sign, why))
                votes = price_votes(s, h + s)
                self.assertEqual([v.name for v in votes], ["REVERSAL"], sign)
                self.assertEqual(1 if votes[0].score > 0 else -1, sign)
        finally:
            cfg.REVERSAL_ENABLED = old

    def test_no_reversal_without_stretch(self):
        h, s = trend_scenario(1)
        self.assertEqual(score_reversal(1, s, h + s)[0], 0.0)
        self.assertEqual(score_reversal(-1, s, h + s)[0], 0.0)

    def test_reversal_can_be_disabled(self):
        h, s = reversal_scenario(1)
        old = cfg.REVERSAL_ENABLED
        try:
            cfg.REVERSAL_ENABLED = False
            self.assertEqual(price_votes(s, h + s), [])
        finally:
            cfg.REVERSAL_ENABLED = old

    def test_reversal_stop_is_behind_the_swing(self):
        h, s = reversal_scenario(1)
        d = invalidation_distance("CALL", s, h + s, s[-1].c, "REVERSAL")
        self.assertGreater(d, s[-1].c - min(c.l for c in (h + s)[-cfg.REV_STOP_BARS:]) - 1e-9)


class TestConfirmedAlertGate(unittest.TestCase):
    def test_additional_setups_are_enabled_by_default(self):
        self.assertTrue(cfg.ORB_ENABLED)
        self.assertTrue(cfg.VWAP_SETUP_ENABLED)
        self.assertTrue(cfg.SQUEEZE_ENABLED)
        self.assertTrue(cfg.REVERSAL_ENABLED)
        self.assertGreaterEqual(cfg.REVERSAL_MIN_ADX, 10.0)
        self.assertTrue(cfg.MOMENTUM_EXPLOSION_ENABLED)

    def test_only_final_score_threshold_is_active(self):
        self.assertFalse(cfg.EARLY_ENABLED)
        self.assertEqual(cfg.STAGE1_MIN_SCORE, 0.0)
        self.assertEqual(cfg.CANDIDATE_MIN_PRICE_SCORE, 0.0)
        self.assertEqual(cfg.ALERT_SCORE_MIN, 8.0)



# ---------------------------------------------------------------- ORB / VWAP / SQUEEZE, confluence, adaptive, report
from datetime import date  # noqa: E402

from db import DB  # noqa: E402
from tests.db_test_utils import make_test_db  # noqa: E402
from engine import report as rpt  # noqa: E402
from engine.adaptive import Adaptive, delta_for  # noqa: E402
from models import Signal  # noqa: E402
from strategies.setups import Ctx, score_orb, score_squeeze, score_vwap  # noqa: E402


def _base(sign, seed=3, sd=15):
    rng = random.Random(seed)
    steps = [rng.gauss(0, sd) for _ in range(150)]
    h1 = mk_candles(25000, [sign * x for x in steps[:75]], day=TODAY - timedelta(days=2))
    h2 = mk_candles(h1[-1].c, [sign * x for x in steps[75:]], day=TODAY - timedelta(days=1))
    return h1 + h2


def _choppy(sign, seed=5, sd=14):
    rng = random.Random(seed)
    steps = [(-1) ** i * abs(rng.gauss(0, sd)) for i in range(150)]
    h1 = mk_candles(25000, [sign * x for x in steps[:75]], day=TODAY - timedelta(days=2))
    h2 = mk_candles(h1[-1].c, [sign * x for x in steps[75:]], day=TODAY - timedelta(days=1))
    return h1 + h2


def orb_scenario(sign):
    h = _base(sign)
    s = mk_candles(h[-1].c, [sign * x for x in (10, -6, 8, -3, -4, 2, 1, 30)], [1000] * 7 + [3000])
    return h, s


def vwap_scenario(sign):
    h = _base(sign, seed=4)
    s = mk_candles(h[-1].c, [sign * x for x in (-14, -12, -10, -8, -6, -5, -2, 12, 16, 20)], [1000] * 7 + [1400, 1800, 2600])
    return h, s


def squeeze_scenario(sign):
    h = _choppy(sign)
    rng = random.Random(9)
    day = [(-1) ** i * abs(rng.gauss(0, 1.5)) for i in range(12)] + [36]
    s = mk_candles(h[-1].c, [sign * x for x in day], [1000] * 12 + [3500])
    return h, s


class TestNewSetupTypes(unittest.TestCase):
    def _check(self, make, fn, name):
        flag = {"ORB": "ORB_ENABLED", "VWAP": "VWAP_SETUP_ENABLED", "SQUEEZE": "SQUEEZE_ENABLED"}[name]
        old = getattr(cfg, flag)
        try:
            setattr(cfg, flag, True)  # these tests validate the module, not the production default
            for sign in (1, -1):
                h, s = make(sign)
                pts, why, _ = fn(sign, Ctx(s, h + s))
                self.assertGreaterEqual(pts, cfg.PRICE_MIN_SCORE, (name, sign, why))
                votes = price_votes(s, h + s)
                self.assertEqual([v.name for v in votes], [name], (name, sign))
                self.assertEqual(1 if votes[0].score > 0 else -1, sign)
                self.assertEqual(fn(-sign, Ctx(s, h + s))[0], 0.0)
        finally:
            setattr(cfg, flag, old)

    def test_orb(self):
        self._check(orb_scenario, score_orb, "ORB")

    def test_vwap_reclaim_and_rejection(self):
        self._check(vwap_scenario, score_vwap, "VWAP")

    def test_squeeze(self):
        self._check(squeeze_scenario, score_squeeze, "SQUEEZE")

    def test_orb_only_first_breakout_and_only_in_window(self):
        h, s = orb_scenario(1)
        # same breakout one candle earlier already happened -> a later close above the range is not a fresh ORB
        s2 = mk_candles(h[-1].c, [10, -6, 8, 20, 1, 1, 30], [1000] * 6 + [3000])
        self.assertEqual(score_orb(1, Ctx(s2, h + s2))[0], 0.0)
        old = cfg.ORB_LAST_TIME
        try:
            cfg.ORB_LAST_TIME = (9, 20)
            self.assertEqual(score_orb(1, Ctx(s, h + s))[0], 0.0)
        finally:
            cfg.ORB_LAST_TIME = old

    def test_types_can_be_disabled(self):
        h, s = orb_scenario(1)
        old = cfg.ORB_ENABLED
        try:
            cfg.ORB_ENABLED = False
            self.assertNotIn("ORB", [v.name for v in price_votes(s, h + s)])
        finally:
            cfg.ORB_ENABLED = old

    def test_confluence_bonus_and_reason(self):
        h, s = vwap_scenario(1)
        old = cfg.VWAP_SETUP_ENABLED
        try:
            cfg.VWAP_SETUP_ENABLED = True
            base = price_votes(s, h + s)[0]
            self.assertTrue(base.reason)
            self.assertIn("EMA9", base.reason)
            self.assertIn("ATR", base.reason)
        finally:
            cfg.VWAP_SETUP_ENABLED = old

    def test_chase_penalty_applies_to_stretched_entries(self):
        h, s = orb_scenario(1)
        s2 = list(s)
        last = s2[-1]
        s2[-1] = replace(last, c=last.c + 400, h=last.c + 401)         # absurdly extended candle
        fresh = price_votes(s, h + s)
        stretched = price_votes(s2, h + s2)
        if stretched:
            self.assertLess(abs(stretched[0].score), abs(fresh[0].score))

    def test_conflicting_sides_are_skipped(self):
        import strategies.price as sp
        orig = sp.all_setups
        try:
            sp.all_setups = lambda session, allc: [(80.0, 1, "PRICE", "x"), (76.0, -1, "REVERSAL", "y")]
            h, s = trend_scenario(1)
            self.assertEqual(sp.price_votes(s, h + s), [])
            sp.all_setups = lambda session, allc: [(80.0, 1, "PRICE", "x"), (70.0, -1, "REVERSAL", "y")]
            self.assertEqual(len(sp.price_votes(s, h + s)), 1)
        finally:
            sp.all_setups = orig

    def test_invalidation_kinds(self):
        for make, kind, side in ((orb_scenario, "ORB", "CALL"), (vwap_scenario, "VWAP", "CALL"),
                                 (squeeze_scenario, "SQUEEZE", "CALL")):
            h, s = make(1)
            self.assertGreater(invalidation_distance(side, s, h + s, s[-1].c, kind), 0, kind)


def _sig(strategy, pnl, tier="FULL", day="2026-10-20", conf=75.0, und="NIFTY", hour="10"):
    return Signal(id=f"{strategy}-{pnl}-{day}-{hour}-{und}-{id(object())}", ts=f"{day}T{hour}:05:00+05:30", underlying=und,
                  side="CALL", status="CLOSED", strategy=strategy, tier=tier, pnl_lot=pnl, confidence=conf,
                  result="WIN" if pnl > 0 else "LOSS")


class TestAdaptiveAndReport(unittest.TestCase):
    def setUp(self):
        self.db = make_test_db()
        self.now = __import__("datetime").datetime(2026, 10, 22, 11, 0)

    def test_delta_thresholds(self):
        n = cfg.ADAPT_MIN_N
        self.assertEqual(delta_for(5, 0.1)[0], 0.0)                  # still learning
        self.assertEqual(delta_for(n - 1, 0.1)[0], 0.0)
        self.assertEqual(delta_for(n, 2.0)[0], -3.0)
        self.assertEqual(delta_for(n, 1.1)[0], 0.0)
        self.assertEqual(delta_for(n, 0.7)[0], 6.0)
        self.assertEqual(delta_for(n, 0.3)[0], 12.0)

    def test_underperforming_type_gets_higher_bar_others_unchanged(self):
        old = cfg.ADAPT_MIN_N
        cfg.ADAPT_MIN_N = 12                                         # small fixture; production default is 40
        try:
            for i in range(14):
                self.db.save_signal(_sig("ORB", -500 if i % 4 else 300, day=f"2026-10-{10 + i % 10:02d}", und=f"S{i}"))
                self.db.save_signal(_sig("TREND", 600 if i % 2 else -200, day=f"2026-10-{10 + i % 10:02d}", und=f"T{i}"))
            ad = Adaptive(self.db)
            self.assertGreater(ad.delta("ORB", self.now)[0], 0)
            self.assertLessEqual(ad.delta("TREND", self.now)[0], 0)
            self.assertEqual(ad.delta("VWAP", self.now)[0], 0)       # no data -> untouched
            self.assertGreater(ad.extra_score("ORB", "INDEX", self.now)[0], 0)   # learning tightens the live gate
            self.assertEqual(ad.extra_score("TREND", "INDEX", self.now)[0], 0)   # proven types never LOWER it
            self.assertEqual(ad.extra_score("VWAP", "INDEX", self.now)[0], 0)
        finally:
            cfg.ADAPT_MIN_N = old

    def test_adaptive_can_be_disabled(self):
        for i in range(14):
            self.db.save_signal(_sig("ORB", -500, day="2026-10-15", und=f"S{i}"))
        old = cfg.ADAPT_ENABLED
        try:
            cfg.ADAPT_ENABLED = False
            self.assertEqual(Adaptive(self.db).delta("ORB", self.now)[0], 0.0)
        finally:
            cfg.ADAPT_ENABLED = old

    def test_report_groups(self):
        self.db.save_signal(_sig("ORB", 400, hour="09", conf=85, und="NIFTY"))
        self.db.save_signal(_sig("VWAP", -300, hour="13", conf=66, tier="EARLY", und="RELIANCE"))
        rep = rpt.build(self.db, 30, date(2026, 10, 22))
        self.assertEqual(rep["overall"]["n"], 2)
        self.assertEqual(rep["by_strategy"]["ORB"]["win_pct"], 100.0)
        self.assertEqual(rep["by_tier"]["EARLY"]["n"], 1)
        self.assertIn("Index", rep["by_kind"])
        text = rpt.format_report(rep, {"ORB": "normal"})
        self.assertIn("By signal type", text)
        self.assertIn("ORB", rpt.format_report(rep, compact=True))
        self.assertIn("No closed signals", rpt.format_report(rpt.build(make_test_db(), 30, date(2026, 10, 22))))
