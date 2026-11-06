"""End-to-end: fake provider -> stage1 -> chain -> score -> contract -> alert -> tracker."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from data.failover import FailoverManager  # noqa: E402
from data.instruments import Master, build_underlyings  # noqa: E402
from db import DB  # noqa: E402
from tests.db_test_utils import make_test_db  # noqa: E402
from engine.risk_rules import RiskRules  # noqa: E402
from engine.scanner import Scanner
from v7.sequential import Decision  # noqa: E402
from engine.tracker import Tracker  # noqa: E402
from tests.test_core import FakeTG, TODAY, chain, master_rows, mk_candles, trend_scenario  # noqa: E402
from utils.market_hours import IST  # noqa: E402

# Fixture candles are stamped on TODAY (a fixed past date). The scanner validates option-chain age against the
# wall clock, so without freezing it every fixture chain is "stale" once real time moves on. Freeze the scanner's
# clock to the fixture's scan time so these integration tests exercise the signal path, not the calendar.
FIXTURE_NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 10, 5, 8, tzinfo=IST)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXTURE_NOW if tz is None else FIXTURE_NOW.astimezone(tz)


class FakeProvider:
    name = "upstox"

    def __init__(self, hist, sess, fut, spot_shift=0.0, sign=1):
        self.hist, self.sess, self.fut, self.spot_shift, self.sign = hist, sess, fut, spot_shift, sign

    def spot_intraday(self, u):
        return self.sess

    def fut_intraday(self, u):
        return self.fut

    def vix_intraday(self):
        return mk_candles(14, [-0.1 * self.sign] * 10)       # VIX falls with a Call setup, rises with a Put setup

    def option_chain(self, u, expiry):
        c = chain(spot=self.sess[-1].c + self.spot_shift)
        c.underlying, c.expiry = u.name, expiry
        c.ts = self.sess[-1].ts + timedelta(minutes=5, seconds=8)
        return c

    def premiums(self, contracts, unds=None):
        return {}

    def ping(self):
        return True


def build(sign=1, spot_shift=0.0, fut_vol=1000):
    # Stronger synthetic trend ensures the explicit ADX >= 25 hard gate is exercised on the pass path.
    hist, sess = trend_scenario(sign, drift=1.5, noise=6.0)
    n = len(sess)
    fut = mk_candles(sess[-1].c, [3] * n, vols=fut_vol)
    # Futures bars share the spot bars' timestamps (the scanner matches them by time to build the index VWAP and
    # drops duplicate timestamps as a data-quality guard).
    for i, c in enumerate(fut):
        c.ts = sess[i].ts
    for i, c in enumerate(fut):                  # price and OI both moving with the trade -> fresh buying / selling
        c.oi = 1_000_000 + i * 9_000
        c.c = fut[0].c * (1 + sign * 0.0007 * i)
        c.h = max(c.o, c.c) + 0.5
        c.l = min(c.o, c.c) - 0.5
    now = sess[-1].ts + timedelta(minutes=5, seconds=8)
    prov = FakeProvider(hist, sess, fut, spot_shift, sign)
    tg, db = FakeTG(), make_test_db()
    fo = FailoverManager(prov, FakeProvider(hist, sess, fut, spot_shift, sign))
    fo.secondary.name = "dhan"
    master = Master(master_rows())
    unds = [u for u in build_underlyings(master, TODAY) if u.name == "NIFTY"]
    tracker = Tracker(db, tg, fo, None, {u.name: u for u in unds})
    sc = Scanner(master, unds, fo, db, tg, RiskRules(db), tracker, {"NIFTY": hist})
    # These legacy end-to-end tests exercise contract/risk/tracking after stage 1.
    # The V7 sequential gate has its own focused tests in test_v7_gate.py.
    sc.sequential.evaluate = lambda *a, **k: Decision(
        "UNDERLYING_SIGNAL_CONFIRMED", "CALL" if sign > 0 else "PUT", "test fixture", {"adx": 32.0, "rsi": 60.0 if sign > 0 else 40.0,
                          "plus_di": 35.0 if sign > 0 else 20.0, "minus_di": 20.0 if sign > 0 else 35.0})
    return sc, tg, tracker, now, sess


class TestE2E(unittest.TestCase):
    def setUp(self):
        self._old_setup_flags = {k: getattr(cfg, k) for k in
                                 ("ORB_ENABLED", "VWAP_SETUP_ENABLED", "SQUEEZE_ENABLED", "REVERSAL_ENABLED")}
        self._old_candle_ready_retries = cfg.CANDLE_READY_RETRIES
        # These fixtures return a static provider snapshot. Retrying a static
        # response four times only sleeps 12 seconds per scan and cannot make
        # the just-closed candle appear; readiness retry behavior has separate
        # focused coverage in the V8.1 tests.
        cfg.CANDLE_READY_RETRIES = 0
        # Fixtures explicitly enable all setup modules so the downstream alert gates are exercised consistently.
        cfg.ORB_ENABLED = cfg.VWAP_SETUP_ENABLED = cfg.SQUEEZE_ENABLED = cfg.REVERSAL_ENABLED = True
        clock = patch("engine.scanner.datetime", _FrozenDatetime)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        for key, value in self._old_setup_flags.items():
            setattr(cfg, key, value)
        cfg.CANDLE_READY_RETRIES = self._old_candle_ready_retries


    def test_candidate_watchlist_persists_across_scanner_restart(self):
        sc, tg, tracker, now, sess = build(1)
        sc._watch("NIFTY", "CALL", sess[-1].ts, 0.75, "WATCHING", reasons=["test"], now=now)
        restored = Scanner(sc.master, sc.unds, sc.fo, sc.db, tg, RiskRules(sc.db), tracker, {"NIFTY": []})
        self.assertIn("NIFTY:CALL", restored.candidate_watchlist)
        self.assertEqual(restored.candidate_watchlist["NIFTY:CALL"]["status"], "WATCHING")

    def test_signal_emitted_call_and_put(self):
        for sign, side, ot in ((1, "CALL", "CE"), (-1, "PUT", "PE")):
            sc, tg, tracker, now, _ = build(sign)
            out = sc.run_once(now)
            self.assertEqual(len(out), 1, (side, sc.last_stats))
            self.assertEqual(out[0].side, side)
            self.assertEqual(out[0].opt_type, ot)
            self.assertTrue(any(r.startswith("Trend:") for r in out[0].reasons))
            ind = [r for r in out[0].reasons if all(k in r for k in ("EMA9", "EMA21", "VWAP", "RSI", "ADX"))]
            self.assertTrue(ind, out[0].reasons)
            self.assertIn("Score:</b>", tg.msgs[-1])
            self.assertIn("/10", tg.msgs[-1])
            self.assertGreaterEqual(out[0].confidence, cfg.ALERT_SCORE_MIN)
            self.assertLessEqual(out[0].confidence, 10.0)
            self.assertIn("NIFTY", tracker.active[out[0].id][0].underlying)
            # the stop is the EMA21/VWAP invalidation level, clamped to the allowed ATR range
            self.assertGreater(abs(out[0].spot - out[0].und_sl), 0)
            self.assertEqual(1 if out[0].und_t1 > out[0].spot else -1, 1 if side == "CALL" else -1)
            self.assertIn("EMA9", tg.msgs[-1])                          # the Telegram alert carries the indicator line
            # A still-active signal for the same underlying must block duplicates.
            self.assertEqual(len(sc.run_once(now + timedelta(minutes=5))), 0)

    def test_normal_engine_runs_without_legacy_strategy_vote(self):
        sc, tg, tracker, now, sess = build(1)
        with patch("engine.scanner.price_votes", return_value=[]):
            cands, _ = sc.stage1(now)
        self.assertEqual(len(cands), 1, sc._stage1_diagnostics)
        self.assertEqual(cands[0]["votes"][0].name, "NORMAL_TREND")
        self.assertIn("NIFTY", sc.v7_diagnostics)
        self.assertEqual(sc.v7_diagnostics["NIFTY"]["status"], "UNDERLYING_SIGNAL_CONFIRMED")

    def test_index_vwap_uses_futures_volume(self):
        sc, tg, tracker, now, sess = build(1, fut_vol=[1000] * 9 + [8000])
        cands, _ = sc.stage1(now)
        self.assertEqual(len(cands), 1)
        vols = [c.v for c in cands[0]["session"]]
        self.assertEqual(vols, [1000] * 9 + [8000])                       # index spot candles now carry futures volume
        self.assertEqual(sc._stage1_diagnostics["index_vwap_twap_fallback"], 0)
        self.assertEqual(len(cands[0]["fut"]), len(sess))                 # the same futures candles are reused in stage 2

    def test_index_vwap_falls_back_to_twap_without_futures_volume(self):
        sc, tg, tracker, now, sess = build(1, fut_vol=0)
        cands, _ = sc.stage1(now)
        self.assertEqual(sc._stage1_diagnostics["index_vwap_twap_fallback"], 1)
        self.assertEqual(len(cands), 1)                                   # still trades; VWAP just stays time-weighted
        self.assertEqual([c.v for c in cands[0]["session"]], [c.v for c in sess])   # spot volume left untouched

    def test_stage2_reuses_stage1_futures_candles(self):
        sc, tg, tracker, now, sess = build(1)
        calls = []
        orig = sc.fo.primary.fut_intraday
        sc.fo.primary.fut_intraday = lambda u: (calls.append(1), orig(u))[1]
        out = sc.run_once(now)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(calls), 1)                                   # fetched once in stage 1, not again in stage 2

    def test_manual_or_cron_scan_outside_signal_window_is_blocked(self):
        sc, tg, tracker, now, sess = build(1)
        after_cutoff = now.replace(hour=15, minute=0, second=0, microsecond=0)
        self.assertEqual(sc.run_once(after_cutoff), [])
        self.assertEqual(sc.last_stats["rejects"], {"outside_signal_window": 1})
        self.assertEqual(sc.last_stats["signals"], 0)

    def test_stale_spot_candles_are_skipped(self):
        sc, tg, tracker, now, sess = build(1)
        for c in sess:
            c.ts -= timedelta(minutes=20)
        self.assertEqual(sc.run_once(now), [])
        self.assertEqual(sc.last_stats["stage1_diagnostics"]["stale_candles"], 1)

    def test_reversal_since_candle_close_is_skipped(self):
        sc, tg, tracker, now, sess = build(1, spot_shift=-30.0)     # spot already fell back well below the signal close
        self.assertEqual(sc.run_once(now), [])
        self.assertIn("ADVERSE_MOVE_AFTER_CONFIRMATION", sc.last_stats["rejects"])

    def test_chase_is_skipped(self):
        sc, tg, tracker, now, sess = build(1, spot_shift=+30.0)     # spot already ran away from the signal close
        self.assertEqual(sc.run_once(now), [])
        self.assertIn("PRICE_EXTENDED_AFTER_CONFIRMATION", sc.last_stats["rejects"])

    def test_oi_wall_is_not_an_alert_approval_gate(self):
        sc, tg, tracker, now, sess = build(1)
        out = sc.run_once(now)
        self.assertEqual(len(out), 1)
        self.assertNotIn("no room to OI wall", sc.last_stats["rejects"])


if __name__ == "__main__":
    unittest.main()
