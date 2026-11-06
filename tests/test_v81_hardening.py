"""V8.1 hardening: error isolation, closed-candle readiness wait, history backfill, first-scan report.

Runs against the shipped defaults (STRATEGY_MODE=SIMPLE), like the other test_simple* files.
"""
import os
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from tests.test_scanner_e2e import FIXTURE_NOW, _FrozenDatetime, build  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        clock = patch("engine.scanner.datetime", _FrozenDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        self._saved = {k: getattr(cfg, k) for k in (
            "CANDLE_READY_RETRIES", "CANDLE_READY_WAIT_SEC", "CANDLE_READY_MAX_WAIT_SEC",
            "HISTORY_BACKFILL_ENABLED", "HISTORY_BACKFILL_PER_SCAN", "HISTORY_BACKFILL_MAX_ATTEMPTS")}
        self.addCleanup(lambda: [setattr(cfg, k, v) for k, v in self._saved.items()])
        cfg.CANDLE_READY_WAIT_SEC = 0.0            # never really sleep in tests


class TestErrorIsolation(_Base):
    def test_candidate_crash_does_not_abort_scan(self):
        sc, tg, tracker, now, _ = build(1)
        calls = {"n": 0}
        real = sc._evaluate_locked

        def boom(c, vix, now_, pre=None):
            calls["n"] += 1
            raise ValueError("odd provider field")
        sc._evaluate_locked = boom
        out = sc.run_once(now)                      # must not raise
        self.assertEqual(out, [])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sc.last_stats["evaluation_errors"], 1)
        self.assertEqual(sc._rej.get("evaluation error"), 1)
        # The same candle evaluated normally afterwards still produces the alert.
        sc._evaluate_locked = real
        out = sc.run_once(now)
        self.assertEqual(len(out), 1)

    def test_rule_crash_is_counted_and_skipped(self):
        sc, tg, tracker, now, _ = build(1)
        with patch.object(cfg, "STRATEGY_MODE", "SIMPLE"), patch.object(sc, "_simple_candidate", side_effect=RuntimeError("bad candle")):
            out = sc.run_once(now)
        self.assertEqual(out, [])
        self.assertEqual(sc._stage1_diagnostics["evaluation_errors"], 1)
        self.assertIn("rule evaluation error", " ".join(sc._symbol_rejections["NIFTY"]["reasons"]))


class TestClosedCandleReadiness(_Base):
    def _late_provider(self, sc, sess, publish_on_call):
        """Provider that withholds the just-closed candle until the Nth spot_intraday call."""
        real = sc.fo.primary.spot_intraday if hasattr(sc.fo, "primary") else None
        state = {"calls": 0}

        def spot(u):
            state["calls"] += 1
            return sess if state["calls"] >= publish_on_call else sess[:-1]
        sc.fo.call = lambda method, *a, **k: spot(*a) if method == "spot_intraday" else getattr(
            sc.fo.primary, method)(*a, **k)
        return state

    def test_late_candle_is_waited_for(self):
        sc, tg, tracker, now, sess = build(1)
        cfg.CANDLE_READY_RETRIES = 4
        state = self._late_provider(sc, sess, publish_on_call=3)
        out = sc.run_once(now)
        self.assertGreaterEqual(state["calls"], 3)
        self.assertEqual(sc._stage1_diagnostics["stale_candles"], 0)
        self.assertEqual(len(out), 1, sc.last_stats.get("rejects"))

    def test_without_wait_the_symbol_is_stale(self):
        sc, tg, tracker, now, sess = build(1)
        cfg.CANDLE_READY_RETRIES = 0                # old behaviour
        self._late_provider(sc, sess, publish_on_call=3)
        out = sc.run_once(now)
        self.assertEqual(out, [])
        self.assertEqual(sc._stage1_diagnostics["stale_candles"], 1)

    def test_gives_up_after_retries_and_stays_bounded(self):
        sc, tg, tracker, now, sess = build(1)
        cfg.CANDLE_READY_RETRIES = 3
        state = self._late_provider(sc, sess, publish_on_call=10 ** 6)
        out = sc.run_once(now)
        self.assertEqual(out, [])
        self.assertEqual(state["calls"], 1 + 3)     # first fetch + exactly the configured retries
        self.assertTrue(sc._ready_state["gave_up"])

    def test_deadline_stops_waiting(self):
        sc, tg, tracker, now, sess = build(1)
        cfg.CANDLE_READY_RETRIES = 5
        cfg.CANDLE_READY_MAX_WAIT_SEC = 1.0         # frozen clock is 8 s past the boundary: already beyond
        state = self._late_provider(sc, sess, publish_on_call=10 ** 6)
        sc.run_once(now)
        self.assertEqual(state["calls"], 1)


class TestHistoryBackfill(_Base):
    def test_short_history_is_refetched_and_signal_returns(self):
        sc, tg, tracker, now, sess = build(1)
        full = list(sc.hist["NIFTY"])
        sc.hist["NIFTY"] = []                       # bootstrap history failed
        sc.fo.call = (lambda orig: lambda method, *a, **k: full if method == "history" else orig(method, *a, **k))(sc.fo.call)
        out = sc.run_once(now)
        self.assertEqual(len(sc.hist["NIFTY"]), len(full))
        self.assertEqual(len(out), 1, sc.last_stats.get("rejects"))

    def test_backfill_is_bounded_per_day(self):
        sc, tg, tracker, now, sess = build(1)
        sc.hist["NIFTY"] = []
        cfg.HISTORY_BACKFILL_MAX_ATTEMPTS = 2
        seen = {"n": 0}
        orig = sc.fo.call

        def call(method, *a, **k):
            if method == "history":
                seen["n"] += 1
                raise RuntimeError("429")
            return orig(method, *a, **k)
        sc.fo.call = call
        for _ in range(4):
            sc.run_once(now + timedelta(seconds=1))
        self.assertEqual(seen["n"], 2)

    def test_backfill_can_be_disabled(self):
        sc, tg, tracker, now, sess = build(1)
        sc.hist["NIFTY"] = []
        cfg.HISTORY_BACKFILL_ENABLED = False
        orig = sc.fo.call
        sc.fo.call = lambda method, *a, **k: (_ for _ in ()).throw(AssertionError("history must not be fetched")) \
            if method == "history" else orig(method, *a, **k)
        sc.run_once(now)                            # no assertion error raised


class TestMinBars(unittest.TestCase):
    def test_default_history_requirement_matches_adx_warmup(self):
        self.assertGreaterEqual(cfg.SIMPLE_MIN_BARS, 2 * 14 + 1 + cfg.SIMPLE_FRESH_BARS)
        self.assertLessEqual(cfg.SIMPLE_MIN_BARS, 40)


if __name__ == "__main__":
    unittest.main()
