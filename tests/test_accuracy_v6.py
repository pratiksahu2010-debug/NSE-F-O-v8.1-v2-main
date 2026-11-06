"""Accuracy v6: realistic outcomes, context gates, adaptive wiring, feature/candle logging, replay + calibration tools."""
import csv
import os
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from db import DB  # noqa: E402
from tests.db_test_utils import make_test_db  # noqa: E402
from engine import context as ctx  # noqa: E402
from engine.adaptive import Adaptive, tod_bucket  # noqa: E402
from engine.outcomes import exit_fill_price, profit_factor, r_multiple, round_trip_cost, summarize  # noqa: E402
from engine.tracker import Tracker  # noqa: E402
from models import Candle, ChainSnapshot, OptionQuote  # noqa: E402
from tests.test_core import FakeTG, make_signal  # noqa: E402
from tests.test_scanner_e2e import FIXTURE_NOW, _FrozenDatetime, build  # noqa: E402
from tools import calibrate, replay  # noqa: E402
from utils.market_hours import IST  # noqa: E402


class Flags:
    """Temporarily set cfg attributes."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(cfg, k)
            setattr(cfg, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(cfg, k, v)


def cndl(ts, c, v=1000.0, oi=0.0):
    return Candle(ts, c, c + 1, c - 1, c, v, oi)


class TestOutcomes(unittest.TestCase):
    def test_exit_fill_is_below_mark_and_uses_spread(self):
        self.assertLess(exit_fill_price(100, 0.0), 100)
        self.assertLess(exit_fill_price(100, 4.0), exit_fill_price(100, 1.0))      # wider spread -> worse fill
        self.assertEqual(exit_fill_price(100, 999.0), exit_fill_price(100, 0.0))   # unknown quote -> minimum slippage

    def test_costs_positive_and_scale_with_turnover(self):
        small, big = round_trip_cost(100, 110, 75), round_trip_cost(400, 440, 75)
        self.assertGreater(small, 40)                  # at least two flat brokerage orders
        self.assertGreater(big, small)

    def test_r_multiple_and_summary(self):
        s = SimpleNamespace(pnl_lot=-3000.0, max_loss_lot=3000.0, cost_lot=15000.0)
        self.assertAlmostEqual(r_multiple(s), -1.0)
        legacy = SimpleNamespace(pnl_lot=450.0, max_loss_lot=0.0, cost_lot=15000.0)  # falls back to cost*MAX_SL_PCT
        self.assertAlmostEqual(r_multiple(legacy), 450.0 / (15000.0 * cfg.MAX_SL_PCT))
        out = summarize([2.0, -1.0, -1.0, 1.0])
        self.assertEqual((out["n"], out["win_pct"], out["max_lose_streak"]), (4, 50.0, 2))
        self.assertAlmostEqual(profit_factor([2.0, -1.0]), 2.0)


class TestTrackerRealism(unittest.TestCase):
    def setUp(self):
        self.t = Tracker(make_test_db(), FakeTG(), None, None, {})

    def test_time_exit_without_quote_uses_last_known_premium_not_entry(self):
        s = make_signal()
        self.t.active[s.id] = (s, None)
        self.t.evaluate(s, 150.0, None, datetime(2026, 10, 15, 11, 0, tzinfo=IST))      # last real price: 150 (a loss)
        self.t._premium = lambda c: None                                               # then quotes disappear
        self.t._refresh_rest = lambda contracts: None
        self.t.step(datetime(2026, 10, 15, 15, 20, tzinfo=IST))
        self.assertEqual(s.status, "CLOSED")
        self.assertLess(s.exit_price, s.ltp)                                           # not marked flat at entry
        self.assertLess(s.pnl_lot, 0)

    def test_realistic_exits_can_be_disabled(self):
        with Flags(REALISTIC_EXITS=False):
            s = make_signal()
            self.t.active[s.id] = (s, None)
            self.t.evaluate(s, 291, None, datetime(2026, 10, 15, 11, 0, tzinfo=IST))
            self.assertAlmostEqual(s.pnl_lot, 91 * 75)


class TestContext(unittest.TestCase):
    def feats(self, **kw):
        base = {"side_sign": 1, "htf": 1, "mkt_bias": 1, "rs_pct": 0.3, "tod_vol_ratio": 1.4,
                "wall_dominant": False, "wall_dist_atr": None, "wall_strike": None, "fut_oi_chg_pct": None}
        base.update(kw)
        return base

    def test_clean_context_passes(self):
        self.assertIsNone(ctx.veto_from(self.feats(), "STOCK", False))

    def test_htf_and_market_opposition_veto_but_not_reversals(self):
        self.assertEqual(ctx.veto_from(self.feats(htf=-1), "INDEX", False)[0], "15m trend opposed")
        self.assertEqual(ctx.veto_from(self.feats(mkt_bias=-1), "STOCK", False)[0], "NIFTY trend opposed")
        self.assertIsNone(ctx.veto_from(self.feats(htf=-1, mkt_bias=-1, rs_pct=-1.0), "STOCK", True))   # counter-trend by design
        self.assertIsNone(ctx.veto_from(self.feats(mkt_bias=-1), "INDEX", False))                       # NIFTY gate is for stocks
        with Flags(HTF_VETO_ENABLED=False):
            self.assertIsNone(ctx.veto_from(self.feats(htf=-1), "INDEX", False))

    def test_relative_strength_is_side_aware(self):
        self.assertEqual(ctx.veto_from(self.feats(rs_pct=-0.5), "STOCK", False)[0], "weak relative strength")   # CALL lagging
        self.assertIsNone(ctx.veto_from(self.feats(side_sign=-1, htf=-1, mkt_bias=-1, rs_pct=-0.5), "STOCK", False))  # PUT lagging = fine
        self.assertEqual(ctx.veto_from(self.feats(side_sign=-1, htf=-1, mkt_bias=-1, rs_pct=0.5), "STOCK", False)[0],
                         "weak relative strength")                                                       # PUT but stock outperforming

    def test_time_of_day_volume(self):
        d0 = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        days = [cndl(d0 + timedelta(days=i), 100, v=1000) for i in range(4)]
        today = cndl(d0 + timedelta(days=4), 100, v=500)
        self.assertAlmostEqual(ctx.tod_vol_ratio(days + [today]), 0.5)
        self.assertEqual(ctx.veto_from(self.feats(tod_vol_ratio=0.5), "INDEX", False)[0], "thin time-of-day volume")
        self.assertIsNone(ctx.tod_vol_ratio(days[:2] + [today]))                  # too few comparable days -> neutral
        self.assertIsNone(ctx.tod_vol_ratio([cndl(d0, 100, v=0)]))                # no volume -> neutral

    def _chain(self, spot=25010.0, wall_oi=900_000):
        rows = {}
        for s in range(24800, 25300, 50):
            for t in ("CE", "PE"):
                oi = 100_000
                if t == "CE" and s == 25050:
                    oi = wall_oi
                rows[(float(s), t)] = OptionQuote(float(s), t, ltp=100, oi=oi)
        return ChainSnapshot("NIFTY", date(2026, 10, 20), spot, 1.0, rows)

    def test_dominant_call_wall_close_above_blocks_call_only(self):
        ch = self._chain()
        w = ctx.wall_info(ch, "CALL", 25010.0, 30.0)
        self.assertTrue(w["dominant"])
        self.assertAlmostEqual(w["dist_atr"], 40 / 30.0, places=2)
        f = ctx.compute_features("INDEX", "CALL", [], [], ch, 45.0, 25010.0, 1, 0, None)   # 40pts = 0.9 ATR
        self.assertEqual(ctx.veto_from(f, "INDEX", False)[0], "into OI wall")
        far = ctx.compute_features("INDEX", "CALL", [], [], ch, 20.0, 25010.0, 1, 0, None)  # 2 ATR away -> room to run
        self.assertIsNone(ctx.veto_from(far, "INDEX", False))
        put = ctx.compute_features("INDEX", "PUT", [], [], ch, 45.0, 25010.0, -1, 0, None)  # the call wall is not a PUT problem
        self.assertIsNone(ctx.veto_from(put, "INDEX", False))
        self.assertIsNone(ctx.wall_info(self._chain(wall_oi=150_000), "CALL", 25010.0, 45.0) and
                          ctx.veto_from(ctx.compute_features("INDEX", "CALL", [], [], self._chain(wall_oi=150_000), 45.0,
                                                             25010.0, 1, 0, None), "INDEX", False))   # not dominant -> no veto

    def test_futures_oi_is_diagnostic_only_and_never_vetoes(self):
        t0 = datetime(2026, 10, 15, 10, 0, tzinfo=IST)
        fut = [cndl(t0 + timedelta(minutes=5 * i), 100, oi=1_000_000 - i * 5_000) for i in range(6)]
        change = ctx.fut_oi_change_pct(fut)
        self.assertLess(change, 0)
        f = self.feats(fut_oi_chg_pct=change)
        self.assertIsNone(ctx.veto_from(f, "INDEX", False))
        self.assertIsNone(ctx.veto_from(f, "STOCK", False))


class TestAdaptiveWired(unittest.TestCase):
    def test_tod_bucket(self):
        self.assertEqual(tod_bucket("2026-10-15T11:47:00+05:30"), "tod:11:30")
        self.assertEqual(tod_bucket(""), "")

    def test_kind_bucket_is_used_and_never_lowers_the_bar(self):
        db = make_test_db()
        with Flags(ADAPT_MIN_N=10):
            for i in range(12):
                s = make_signal()
                s.id, s.strategy, s.kind, s.status = f"B{i}", "ORB", "STOCK", "CLOSED"
                s.pnl_lot, s.ts = (-3000.0 if i % 5 else 600.0), f"2026-10-{10 + i % 9:02d}T10:15:00+05:30"
                db.save_signal(s)
                g = make_signal()
                g.id, g.strategy, g.kind, g.status, g.pnl_lot = f"G{i}", "ORB", "INDEX", "CLOSED", 4500.0
                g.ts = f"2026-10-{10 + i % 9:02d}T13:40:00+05:30"
                db.save_signal(g)
            ad = Adaptive(db)
            now = datetime(2026, 10, 22, 10, 15)
            self.assertGreater(ad.extra_score("ORB", "STOCK", now)[0], 0)      # stock ORB is bad -> higher bar
            self.assertEqual(ad.extra_score("ORB", "INDEX", datetime(2026, 10, 22, 13, 40))[0], 0)   # index ORB is proven: no relaxation


class TestPipelineLogging(unittest.TestCase):
    def setUp(self):
        clock = patch("engine.scanner.datetime", _FrozenDatetime)
        clock.start()
        self.addCleanup(clock.stop)

    def test_signal_logs_features_and_archives_candles(self):
        sc, tg, tracker, now, sess = build(1)
        out = sc.run_once(now)
        self.assertEqual(len(out), 1, sc.last_stats)
        sig = out[0]
        self.assertEqual(sig.kind, "INDEX")
        feats = sc.db.features_since("2000-01-01")[sig.id]
        for k in ("score", "strategy", "kind", "htf", "adx", "rsi", "tod_vol_ratio", "score_components"):
            self.assertIn(k, feats)
        self.assertEqual(feats["kind"], "INDEX")
        rows = sc.db.candles_between("NIFTY", "2000-01-01", "2100-01-01")
        self.assertGreaterEqual(len(rows), len(sess) - 1)
        sc.run_once(now)                                                     # re-scan must not duplicate rows
        self.assertEqual(len(sc.db.candles_between("NIFTY", "2000-01-01", "2100-01-01")), len(rows))

    def test_context_gate_blocks_and_reports_the_reason(self):
        sc, tg, tracker, now, sess = build(1)
        sc.mkt_bias = 0
        with patch("engine.scanner.htf_bias", return_value=-1):               # 15m trend against the CALL
            out = sc.run_once(now)
        self.assertEqual(out, [])
        self.assertEqual(sc.last_stats["rejects"].get("15m normal context mismatch"), 1)
        self.assertIn("15m normal context mismatch",
                      sc.last_stats["symbol_diagnostics"]["NIFTY"]["reasons"][0])

    def test_archive_can_be_disabled(self):
        with Flags(CANDLE_ARCHIVE_ENABLED=False):
            sc, tg, tracker, now, sess = build(1)
            sc.run_once(now)
            self.assertEqual(sc.db.candles_between("NIFTY", "2000-01-01", "2100-01-01"), [])


class TestDbHelpers(unittest.TestCase):
    def test_candles_roundtrip_and_symbols(self):
        db = make_test_db()
        t0 = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
        cs = [cndl(t0 + timedelta(minutes=5 * i), 100 + i, v=10 + i, oi=5) for i in range(3)]
        db.save_candles("RELIANCE", cs)
        db.save_candles("RELIANCE", cs)                                      # idempotent
        got = db.candles_between("RELIANCE", "2026-10-15", "2026-10-15")
        self.assertEqual([c.c for c in got], [100, 101, 102])
        self.assertEqual(got[0].ts, cs[0].ts)
        self.assertEqual(db.candle_symbols(), ["RELIANCE"])
        self.assertEqual(db.candles_between("RELIANCE", "2026-10-16", "2026-10-20"), [])

    def test_features_roundtrip(self):
        db = make_test_db()
        db.save_features("S1", "2026-10-15", {"score": 8.5, "x": None})
        db.save_features("S1", "2026-10-15", {"score": 9.0})
        self.assertEqual(db.features_since("2026-10-15"), {"S1": {"score": 9.0}})
        self.assertEqual(db.features_since("2026-10-16"), {})


class TestReplayTool(unittest.TestCase):
    def test_outcome_geometry(self):
        mk = lambda o, h, l, c, i: Candle(datetime(2026, 10, 15, 10, 0, tzinfo=IST) + timedelta(minutes=5 * i), o, h, l, c)
        r, why, k = replay.outcome(1, 100, 2, 3, 5, [mk(100, 101, 99.5, 100.5, 0), mk(100.5, 105.5, 100, 105, 1)])
        self.assertEqual((why, k), ("T2", 2))
        self.assertAlmostEqual(r, 2.5)
        r, why, _ = replay.outcome(1, 100, 2, 3, 5, [mk(100, 103.5, 97.5, 98, 0)])      # both touched -> stop first
        self.assertEqual((why, round(r, 2)), ("SL", -1.0))
        r, why, _ = replay.outcome(-1, 100, 2, 3, 5, [mk(100, 100.5, 96.5, 97, 0), mk(97, 100.2, 96.9, 100, 1)])
        self.assertEqual(why, "BE")                                                    # T1 then back to entry
        self.assertAlmostEqual(r, 0.0)
        late = Candle(datetime(2026, 10, 15, 15, 10, tzinfo=IST), 100, 100.5, 99.5, 100.4, 0)
        r, why, _ = replay.outcome(1, 100, 2, 3, 5, [late])
        self.assertEqual(why, "TIME")
        self.assertAlmostEqual(r, 0.2)

    def test_cli_runs_on_csv_and_on_empty_dir(self):
        rnd = random.Random(7)
        with tempfile.TemporaryDirectory() as d:
            px, rows = 1000.0, []
            for day in range(8):
                t = datetime(2026, 10, 5 + day, 9, 15, tzinfo=IST)
                if t.weekday() >= 5:
                    continue
                for i in range(75):
                    o = px
                    px *= 1 + rnd.gauss(0.0004, 0.002)
                    rows.append((t + timedelta(minutes=5 * i), o, max(o, px) * 1.0007, min(o, px) * 0.9993, px, rnd.randint(500, 3000)))
            with open(os.path.join(d, "TESTSTOCK.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["ts", "o", "h", "l", "c", "v"])
                for r in rows:
                    w.writerow([r[0].isoformat(), *r[1:]])
            out = os.path.join(d, "out.csv")
            with Flags(RESEARCH_MODE=True):
                self.assertEqual(replay.main(["--csv-dir", d, "--min-score", "5", "--train-frac", "0.6", "--out", out]), 0)
            self.assertEqual(replay.main(["--csv-dir", tempfile.mkdtemp()]), 1)


class TestCalibrateTool(unittest.TestCase):
    def test_collect_and_tables_and_suggestions(self):
        db = make_test_db()
        today = date.today()
        for i in range(40):
            s = make_signal()
            s.id, s.strategy, s.kind, s.status = f"C{i}", "VWAP", "STOCK", "CLOSED"
            s.pnl_lot = -3000.0 if i % 3 else 1000.0
            s.ts = f"{today.isoformat()}T10:{i % 60:02d}:00+05:30"
            db.save_signal(s)
            db.save_features(s.id, today.isoformat(), {"score": 8.0 + (i % 4) * 0.5, "htf": 1, "side_sign": 1, "adx": 27, "spread_pct": 1.5})
        rows = calibrate.collect(db, 5)
        self.assertEqual(len(rows), 40)
        res = calibrate.tables(rows)
        self.assertIn("Score band", res)
        self.assertEqual(res["Strategy|kind"]["VWAP|STOCK"]["n"], 40)
        text = " ".join(calibrate.suggestions(rows, res))
        self.assertIn("VWAP|STOCK", text)                       # negative expectancy bucket is flagged


class TestConfigSafety(unittest.TestCase):
    def test_alert_floor_is_8_unless_research_mode(self):
        self.assertEqual(cfg.ALERT_SCORE_FLOOR, 0.0 if cfg.RESEARCH_MODE else 8.0)
        self.assertGreaterEqual(cfg.ALERT_SCORE_MIN, 8.0 if not cfg.RESEARCH_MODE else 0.0)
        self.assertFalse(cfg.RESEARCH_MODE, "RESEARCH_MODE must be off for the live alert service")

    def test_adaptive_min_sample_is_not_noise_level(self):
        self.assertGreaterEqual(cfg.ADAPT_MIN_N, 30)


if __name__ == "__main__":
    unittest.main()

class TestV3TargetLifecycle(unittest.TestCase):
    def test_v3_tracks_t1_t2_then_closes_at_t3(self):
        tracker = Tracker(make_test_db(), FakeTG(), None, None, {})
        signal = make_signal()
        signal.t3 = 320.0
        tracker.active[signal.id] = (signal, None)
        now = datetime(2026, 10, 15, 11, 0, tzinfo=IST)
        with Flags(V3_PROFILE_ENABLED=True):
            tracker.evaluate(signal, 330.0, None, now)
            self.assertEqual((signal.status, signal.cur_sl), ("T1_HIT", signal.ltp))
            tracker.evaluate(signal, 330.0, None, now)
            self.assertEqual((signal.status, signal.cur_sl), ("T2_HIT", signal.t1))
            tracker.evaluate(signal, 330.0, None, now)
            self.assertEqual((signal.status, signal.exit_reason, signal.result), ("CLOSED", "T3", "WIN"))
            self.assertNotIn(signal.id, tracker.active)
