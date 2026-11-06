"""SIMPLE mode: EMA 9/21 + session VWAP + RSI(14) + ADX(14) >= threshold, end to end.

This file runs against the shipped defaults (see tests/conftest.py): STRATEGY_MODE=SIMPLE, live intrabar on.
"""
import os
import random
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from indicators.core import atr  # noqa: E402
from strategies.simple_signal import evaluate_simple  # noqa: E402
from tests.test_core import TODAY, mk_candles, trend_scenario  # noqa: E402
from tests.test_scanner_e2e import FIXTURE_NOW, _FrozenDatetime, build  # noqa: E402
from utils.market_hours import IST  # noqa: E402


def scenario(sign=1):
    return trend_scenario(sign, drift=1.5, noise=6.0)


def at_bar(sign, k, **kw):
    hist, sess = scenario(sign)
    s = sess[:k]
    return evaluate_simple(s, hist + s, **kw)


class TestDefaults(unittest.TestCase):
    def test_shipped_defaults(self):
        self.assertEqual(cfg.STRATEGY_MODE, "SIMPLE")
        self.assertEqual(cfg.SIMPLE_ADX_MIN, 25.0)
        self.assertEqual((cfg.SIMPLE_RSI_CALL_MIN, cfg.SIMPLE_RSI_CALL_MAX), (52.0, 68.0))
        self.assertEqual((cfg.SIMPLE_RSI_PUT_MIN, cfg.SIMPLE_RSI_PUT_MAX), (32.0, 48.0))
        self.assertEqual(cfg.SIMPLE_MAX_EXTENSION_ATR, 1.5)
        self.assertEqual(cfg.SIMPLE_MIN_RR_T1, 1.5)
        self.assertTrue(cfg.LIVE_INTRABAR_ENABLED)


class TestSimpleRule(unittest.TestCase):
    def test_call_and_put_fire_on_clean_trends(self):
        for sign, side in ((1, 1), (-1, -1)):
            r = at_bar(sign, 3)
            self.assertEqual(r["side"], side, r["reason"])
            m = r["metrics"]
            self.assertGreaterEqual(m["adx"], 25.0)
            for key in ("EMA9", "EMA21", "VWAP", "RSI", "ADX"):
                self.assertIn(key, r["reason"])
            if side == 1:
                self.assertGreater(m["ema9"], m["ema21"])
                self.assertGreater(m["price"], m["vwap"])
                self.assertGreaterEqual(m["rsi"], cfg.SIMPLE_RSI_CALL_MIN)
            else:
                self.assertLess(m["ema9"], m["ema21"])
                self.assertLess(m["price"], m["vwap"])
                self.assertLessEqual(m["rsi"], cfg.SIMPLE_RSI_PUT_MAX)

    def test_adx_threshold_is_the_gate(self):
        base = at_bar(1, 3)
        adx_now = base["metrics"]["adx"]
        above = at_bar(1, 3, adx_min=adx_now + 0.5)
        self.assertEqual(above["side"], 0)
        self.assertIn("CALL:adx", above["gate_failures"])
        self.assertEqual(at_bar(1, 3, adx_min=adx_now - 0.5)["side"], 1)
        self.assertEqual(at_bar(-1, 3, adx_min=99.0)["side"], 0)

    def test_rsi_zone_is_a_gate(self):
        r = at_bar(1, 3, rsi_call_min=99.0)
        self.assertEqual(r["side"], 0)
        self.assertIn("CALL:rsi", r["gate_failures"])
        r = at_bar(1, 3, rsi_call_max=40.0)                      # overbought cap below the live RSI
        self.assertEqual(r["side"], 0)
        r = at_bar(-1, 3, rsi_put_max=10.0)
        self.assertEqual(r["side"], 0)
        self.assertIn("PUT:rsi", r["gate_failures"])


    def test_opposing_15m_context_rejects_but_neutral_context_does_not_veto(self):
        opposed = at_bar(1, 3, context_15m=-1)
        self.assertEqual(opposed["side"], 0)
        self.assertIn("CALL:15m_context_opposed", opposed["gate_failures"])
        neutral = at_bar(1, 3, context_15m=0)
        self.assertEqual(neutral["side"], 1, neutral["reason"])

    def test_vwap_side_is_a_gate(self):
        # Candle 7 of the fixture closes below VWAP while EMA9 > EMA21: no CALL.
        r = at_bar(1, 7)
        self.assertEqual(r["side"], 0)
        self.assertIn("CALL:vwap", r["gate_failures"])
        r = at_bar(-1, 7)
        self.assertEqual(r["side"], 0)
        self.assertIn("PUT:vwap", r["gate_failures"])

    def test_signal_only_while_fresh(self):
        stale = at_bar(1, 5)
        self.assertEqual(stale["side"], 0)
        self.assertIn("CALL:not_fresh", stale["gate_failures"])
        self.assertEqual(at_bar(1, 5, fresh_bars=10, rsi_call_min=0.0, rsi_call_max=100.0, max_entry_extension_atr=99.0)["side"], 1)
        # Returns after a VWAP dip: a new, young setup.
        r = at_bar(1, 9, rsi_call_min=0.0, rsi_call_max=100.0, max_entry_extension_atr=99.0)
        self.assertEqual(r["side"], 1)
        self.assertEqual(r["metrics"]["bars_in_signal"], 1)
        self.assertTrue(any("VWAP" in t for t in r["metrics"]["trigger"]))

    def test_flat_market_gives_nothing(self):
        hist = mk_candles(25000, [random.Random(3).choice((-1.0, 1.0)) * 0.5 for _ in range(150)],
                          day=TODAY - timedelta(days=1))
        sess = mk_candles(hist[-1].c, [0.2, -0.2, 0.1, -0.1, 0.2, -0.2], [1000] * 6)
        r = evaluate_simple(sess, hist + sess)
        self.assertEqual(r["side"], 0)
        self.assertTrue(r["gate_failures"])

    def test_not_enough_data(self):
        hist, sess = scenario(1)
        r = evaluate_simple(sess[:2], hist + sess[:2])
        self.assertEqual(r["side"], 0)
        self.assertEqual(r["gate_failures"], ["session_bars"])
        r = evaluate_simple(sess[:5], sess[:5])
        self.assertEqual(r["side"], 0)
        self.assertEqual(r["gate_failures"], ["history"])

    def test_no_signal_without_vwap(self):
        hist, sess = scenario(1)
        r = evaluate_simple(sess[:5], hist + sess[:5][:-1] + [hist[-1]])   # latest candle not in the session
        self.assertEqual(r["side"], 0)


class TestScannerSimpleMode(unittest.TestCase):
    def setUp(self):
        clock = patch("engine.scanner.datetime", _FrozenDatetime)
        clock.start()
        self.addCleanup(clock.stop)

    def test_alert_for_call_and_put(self):
        for sign, side, ot in ((1, "CALL", "CE"), (-1, "PUT", "PE")):
            sc, tg, tracker, now, _ = build(sign)
            out = sc.run_once(now)
            self.assertEqual(len(out), 1, (side, sc.last_stats.get("rejects"), sc.last_stats.get("no_setup_reasons")))
            sig = out[0]
            self.assertEqual((sig.side, sig.opt_type, sig.strategy), (side, ot, "SIMPLE_TREND"))
            msg = tg.msgs[-1]
            self.assertIn("EMA9", msg)
            self.assertIn("ADX", msg)
            self.assertIn("Rule:", msg)
            self.assertNotIn("minimum 8.0/10", msg)
            # SIMPLE Normal uses a nearby structural invalidation level, with the
            # existing minimum ATR noise buffer applied by RiskRules.
            from strategies.price import invalidation_distance
            context = sc.live_context["NIFTY"]
            a5 = atr(context["allc"], 14)[-1]
            raw_stop = invalidation_distance(side, context["session"], context["allc"], sig.spot, "SIMPLE_TREND")
            expected_stop = max(raw_stop, cfg.SL_MIN_ATR * a5)
            self.assertAlmostEqual(abs(sig.spot - sig.und_sl), expected_stop, delta=0.02 * a5)
            self.assertEqual(1 if sig.und_t1 > sig.spot else -1, 1 if side == "CALL" else -1)
            self.assertIn("NIFTY", tracker.active[sig.id][0].underlying)
            # Same candle again: one open signal per underlying, nothing duplicated.
            self.assertEqual(sc.run_once(now), [])

    def test_stats_report_simple_engine(self):
        sc, _, _, now, _ = build(1)
        sc.run_once(now)
        diag = sc.last_stats["normal_trend_engine"]
        self.assertEqual(diag["engine"], "SIMPLE")
        self.assertEqual((diag["scanned"], diag["detected"], diag["selected"]), (1, 1, 1))

    def test_no_trigger_is_explained(self):
        sc, _, _, now, sess = build(1)
        sc.fo.primary.sess = sess[:7]                       # candle 7 closes below VWAP: no CALL
        out = sc.run_once(now - timedelta(minutes=15))
        self.assertEqual(out, [])
        self.assertIn("CALL:vwap", sc.last_stats["normal_trend_engine"]["gate_failures"])

    def test_stale_chain_is_refetched_not_dropped(self):
        sc, tg, _, now, _ = build(1)
        prov = sc.fo.primary
        original = prov.option_chain
        calls = []

        def aged_first(u, expiry):
            chain = original(u, expiry)
            if not calls:
                chain.ts = FIXTURE_NOW - timedelta(seconds=cfg.MAX_OPTION_CHAIN_AGE_SEC + 30)
            calls.append(1)
            return chain

        prov.option_chain = aged_first
        out = sc.run_once(now)
        self.assertEqual(len(out), 1, sc.last_stats.get("rejects"))
        self.assertGreaterEqual(len(calls), 2)

    def test_chain_that_stays_stale_is_still_rejected(self):
        sc, _, _, now, _ = build(1)
        prov = sc.fo.primary
        original = prov.option_chain

        def always_stale(u, expiry):
            chain = original(u, expiry)
            chain.ts = FIXTURE_NOW - timedelta(seconds=cfg.MAX_OPTION_CHAIN_AGE_SEC + 30)
            return chain

        prov.option_chain = always_stale
        self.assertEqual(sc.run_once(now), [])
        self.assertIn("stale option chain", sc.last_stats["rejects"])

    def test_intrabar_path_is_enabled_by_default(self):
        sc, _, _, _, _ = build(1)
        sc.live_engine = MagicMock()
        sc.ingest_live_tick("NSE_INDEX|Nifty 50", MagicMock(ltp=25000.0, volume=0.0), time.time())
        sc.live_engine.update.assert_called_once()


class TestDataHealthAlert(unittest.TestCase):
    def _bot(self, diag):
        import bot as botmod
        b = botmod.Bot.__new__(botmod.Bot)
        b.tg = MagicMock()
        b.fo = MagicMock(last_error="AuthError: upstox auth 401")
        b.scanner = MagicMock(last_stats={"stage1_diagnostics": diag})
        return b

    def test_alerts_when_most_symbols_have_no_data_then_throttles(self):
        t = datetime(2026, 11, 4, 10, 5, tzinfo=IST)
        b = self._bot({"universe": 100, "fetch_empty_or_failed": 80, "stale_candles": 5, "no_closed_candles": 0})
        b._data_health_alert(t)
        self.assertEqual(b.tg.send.call_count, 1)
        text = b.tg.send.call_args[0][0]
        self.assertIn("85/100", text)
        self.assertIn("AuthError", text)
        b._data_health_alert(t + timedelta(minutes=5))
        self.assertEqual(b.tg.send.call_count, 1)             # inside the cooldown

    def test_silent_when_data_is_fine(self):
        t = datetime(2026, 11, 4, 10, 5, tzinfo=IST)
        b = self._bot({"universe": 100, "fetch_empty_or_failed": 3, "stale_candles": 2, "no_closed_candles": 0})
        b._data_health_alert(t)
        b.tg.send.assert_not_called()


class TestFailureVisibility(unittest.TestCase):
    def test_bootstrap_failure_alert_is_sent_once_per_cooldown(self):
        import bot as botmod
        b = botmod.Bot.__new__(botmod.Bot)
        b.tg = MagicMock()
        b._alert_failure("bootstrap", "Daily bootstrap failed", RuntimeError("PostgreSQL <down> & out"))
        b._alert_failure("bootstrap", "Daily bootstrap failed", RuntimeError("again"))
        self.assertEqual(b.tg.send.call_count, 1)
        text = b.tg.send.call_args[0][0]
        self.assertIn("RuntimeError", text)
        self.assertIn("&lt;down&gt;", text)                   # HTML-escaped for Telegram
        b._alert_failure("other", "Different failure", ValueError("x"))
        self.assertEqual(b.tg.send.call_count, 2)             # separate kinds are throttled separately

    def test_startup_failure_alert_uses_standalone_telegram(self):
        import main as mainmod
        mainmod._STARTUP_ALERT["at"] = None
        with patch("telegram_service.Telegram") as tg_cls:
            mainmod._alert_startup_failure(RuntimeError("PostgreSQL database is required"))
            mainmod._alert_startup_failure(RuntimeError("PostgreSQL database is required"))
        self.assertEqual(tg_cls.return_value.send.call_count, 1)
        self.assertIn("failed to start", tg_cls.return_value.send.call_args[0][0])

    def test_main_loop_reports_a_bot_that_cannot_be_constructed(self):
        """The call site matters as much as the helper: Bot() failing at boot must reach Telegram, not just the log."""
        import signal
        import threading
        import main as mainmod
        old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        alerted = threading.Event()
        timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT))
        try:
            with patch.object(mainmod, "start_server", return_value=MagicMock()), \
                    patch("bot.Bot", side_effect=RuntimeError("PostgreSQL database is required")), \
                    patch.object(mainmod, "_alert_startup_failure", side_effect=lambda exc: alerted.set()) as alert:
                timer.start()
                mainmod.main()
            self.assertTrue(alerted.is_set())
            self.assertIsInstance(alert.call_args[0][0], RuntimeError)
        finally:
            timer.cancel()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)


class TestChainQualityWindow(unittest.TestCase):
    def _chain(self, missing):
        from engine.chain_quality import chain_quality_issues
        from tests.test_core import chain
        c = chain(spot=25010.0)
        calls = sorted((k for k in c.rows if k[1] == "CE"), key=lambda k: abs(k[0] - c.spot))[:5]
        for key in calls[:missing]:
            c.rows[key].bid = 0.0
        return chain_quality_issues, c

    def test_two_one_sided_strikes_out_of_five_do_not_veto(self):
        check, c = self._chain(missing=2)
        self.assertEqual(cfg.CHAIN_ATM_STRIKES, 2)
        self.assertFalse([i for i in check(c, c.spot, side="CALL") if i.startswith("missing_bid_ask")])

    def test_mostly_missing_quotes_still_veto(self):
        check, c = self._chain(missing=5)
        self.assertTrue([i for i in check(c, c.spot, side="CALL") if i.startswith("missing_bid_ask")])


if __name__ == "__main__":
    unittest.main()
