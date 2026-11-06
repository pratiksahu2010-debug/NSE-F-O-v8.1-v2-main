import os
import sqlite3
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg  # noqa: E402
from data.errors import ProviderError  # noqa: E402
from data.failover import FailoverManager  # noqa: E402
from data.instruments import Master, build_underlyings  # noqa: E402
from data.upstox_client import UpstoxProvider  # noqa: E402
from data.dhan_client import DhanProvider  # noqa: E402
from db import DB  # noqa: E402
from engine import scorer  # noqa: E402
from engine.contract_picker import liquidity_ok, pick_contract, pick_expiry  # noqa: E402
from engine.risk_rules import RiskRules  # noqa: E402
from engine.tracker import Tracker  # noqa: E402
from indicators.core import atr, ema, rsi, vwap  # noqa: E402
from models import Candle, ChainSnapshot, OptionQuote, Signal, Underlying  # noqa: E402
from strategies.oi import fut_buildup, pcr_shift  # noqa: E402
from strategies.price import price_score, price_votes  # noqa: E402
from telegram_service import format_signal  # noqa: E402
from utils.market_hours import IST  # noqa: E402


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return DB(connection=conn)

TODAY = date(2026, 10, 15)


def ms(d: date):
    return int(datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp() * 1000)


def mk_candles(start_price, steps, vols=1000, day=TODAY, start="09:15", step_min=5):
    h, m = map(int, start.split(":"))
    t = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
    out, p = [], start_price
    for i, s in enumerate(steps):
        o, c = p, p + s
        v = vols[i] if isinstance(vols, list) else vols
        out.append(Candle(t + timedelta(minutes=step_min * i), o, max(o, c) + 0.5, min(o, c) - 0.5, c, v))
        p = c
    return out


def trend_scenario(sign=1, seed=12, brk=4, drift=1.2, noise=7.0, scale=3.0):
    """Two prior sessions of an orderly trend (noise + pullbacks, ends ~25000, ADX ~27, RSI ~62 / ~38, aligned 15m trend)
    and a today-session that opens with a steady push, pulls back to EMA9 without losing EMA21, then resumes on the
    last candle (`brk` points): a textbook EMA9/21 + VWAP + RSI + ADX pullback entry. sign=-1 is the exact mirror (PUT)."""
    drift, noise, brk = drift * scale, noise * scale, brk * scale       # scale: 5m ATR ~0.08% of price like the real NIFTY
    rng = random.Random(seed)
    steps = [rng.gauss(drift, noise) for _ in range(150)]
    start = 25000 - sign * sum(steps)
    h1 = mk_candles(start, [sign * x for x in steps[:75]], day=TODAY - timedelta(days=2))
    h2 = mk_candles(h1[-1].c, [sign * x for x in steps[75:]], day=TODAY - timedelta(days=1))
    day = [x * scale for x in (2, 3, 2, 3, 2, -2, -3, -1, 3)] + [brk]
    sess = mk_candles(h2[-1].c, [sign * x for x in day], [1000] * len(day))
    return h1 + h2, sess


def cross_scenario(sign=1, scale=3.0):
    """A gentle downtrend reverses at the open into a steady rally: EMA9 crosses above EMA21 a few candles in, with price
    above VWAP, ADX building and RSI rising. sign=-1 is the mirror image (EMA9 crossing below EMA21)."""
    rng = random.Random(4)
    steps = [rng.gauss(-1.5 * scale, 4.0 * scale) for _ in range(150)]
    start = 25000 - sum(steps)
    h1 = mk_candles(start, [sign * x for x in steps[:75]], day=TODAY - timedelta(days=2))
    h2 = mk_candles(h1[-1].c, [sign * x for x in steps[75:]], day=TODAY - timedelta(days=1))
    rng = random.Random(4 * 7)
    day = [(1.5 + rng.uniform(-1.0, 1.0)) * scale for _ in range(8)]
    sess = mk_candles(h2[-1].c, [sign * x for x in day], [1000] * len(day))
    return h1 + h2, sess


def master_rows():
    rows = []
    exps = [date(2026, 10, 20), date(2026, 10, 27), date(2026, 11, 24)]
    for name, spot_key, utype, lot, base, step in [("NIFTY", "NSE_INDEX|Nifty 50", "INDEX", 75, 25000, 50),
                                                  ("BANKNIFTY", "NSE_INDEX|Nifty Bank", "INDEX", 30, 52000, 100),
                                                  ("RELIANCE", "NSE_EQ|INE002A01018", "EQUITY", 500, 1400, 10)]:
        rows.append({"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": name, "underlying_key": spot_key,
                     "underlying_type": utype, "expiry": ms(exps[1]), "lot_size": lot, "instrument_key": f"NSE_FO|F{name}", "tick_size": 5})
        for e in exps:
            for k in range(-5, 6):
                for t in ("CE", "PE"):
                    rows.append({"segment": "NSE_FO", "instrument_type": t, "underlying_symbol": name, "underlying_key": spot_key,
                                 "underlying_type": utype, "expiry": ms(e), "strike_price": base + k * step, "lot_size": lot,
                                 "instrument_key": f"NSE_FO|{name}{e}{base + k * step}{t}", "tick_size": 5,
                                 "trading_symbol": f"{name} {base + k * step} {t} {e:%d %b %y}".upper()})
    return rows


class FakeTG:
    def __init__(self):
        self.msgs = []

    def send(self, m):
        self.msgs.append(m)


class TestIndicators(unittest.TestCase):
    def test_basic(self):
        c = mk_candles(100, [1] * 40)
        self.assertGreater(ema([x.c for x in c], 9)[-1], ema([x.c for x in c], 21)[-1])
        self.assertGreater(atr(c)[-1], 0)
        self.assertGreater(rsi([x.c for x in c])[-1], 70)
        self.assertGreater(vwap(c)[-1], 100)


class TestStrategies(unittest.TestCase):
    def test_price_votes_call_and_put(self):
        for sign in (1, -1):
            hist, sess = trend_scenario(sign)
            votes = price_votes(sess, hist + sess)
            self.assertEqual([v.name for v in votes], ["PRICE"], sign)
            self.assertEqual(1 if votes[0].score > 0 else -1, sign)
            self.assertGreaterEqual(abs(price_score(votes)), cfg.PRICE_MIN_SCORE / 100)
            for token in ("EMA9", "EMA21", "VWAP", "RSI", "ADX", "Vol", "ATR"):
                self.assertIn(token, votes[0].reason)          # every alert shows the essential indicators behind it

    def test_price_votes_cross_scenario(self):
        for sign in (1, -1):
            hist, sess = cross_scenario(sign)
            votes = price_votes(sess, hist + sess)
            self.assertEqual(len(votes), 1, sign)
            self.assertEqual(1 if votes[0].score > 0 else -1, sign)

    def test_fut_buildup_matrix(self):
        def fut(p0, p1, o0, o1):
            c = mk_candles(p0, [(p1 - p0) / 6] * 6 + [0])[:7]
            c[0].oi, c[-1].oi = o0, o1
            c[-1].c = p1
            return c
        self.assertGreater(fut_buildup(fut(100, 101, 1000, 1050)).score, 0.5)    # up/up
        self.assertLess(fut_buildup(fut(100, 99, 1000, 1050)).score, -0.5)       # down/up
        v = fut_buildup(fut(100, 101, 1000, 950)).score
        self.assertTrue(0 < v < 0.5)                                             # up/down (short covering)
        v = fut_buildup(fut(100, 99, 1000, 950)).score
        self.assertTrue(-0.5 < v < 0)                                            # down/down (long unwinding)

    def test_pcr_shift(self):
        snap = ChainSnapshot("NIFTY", TODAY, 25000, 1.2)
        self.assertGreater(pcr_shift(snap, 1.0).score, 0)
        snap.pcr = 0.8
        self.assertLess(pcr_shift(snap, 1.0).score, 0)


class TestScorer(unittest.TestCase):
    def test_combine(self):
        r = scorer.combine({"price": 0.8, "oi": 0.9, "chain": 0.5, "regime": 0.3})
        self.assertEqual(r[0], "CALL")
        self.assertGreaterEqual(r[1], cfg.MIN_CONFIDENCE)
        self.assertIsNone(scorer.combine({"price": 0.1, "oi": 0.9, "chain": 0.9, "regime": 0.9}))   # price must agree
        self.assertIsNone(scorer.combine({"price": 0.4, "oi": 0.0, "chain": 0.0, "regime": 0.0}))   # <2 sources
        r = scorer.combine({"price": -0.8, "oi": -0.9, "chain": -0.5, "regime": -0.3})
        self.assertEqual(r[0], "PUT")


class TestInstruments(unittest.TestCase):
    def setUp(self):
        self.m = Master(master_rows())

    def test_universe_all_indices_and_stocks(self):
        u = build_underlyings(self.m, TODAY)
        names = {x.name: x.kind for x in u}
        self.assertEqual(names, {"NIFTY": "INDEX", "BANKNIFTY": "INDEX", "RELIANCE": "STOCK"})
        nifty = [x for x in u if x.name == "NIFTY"][0]
        self.assertEqual((nifty.lot_size, nifty.strike_step), (75, 50.0))
        self.assertEqual(build_underlyings(self.m, TODAY, stocks=[])[-1].kind, "INDEX")

    def test_expiry_rules(self):
        u = {x.name: x for x in build_underlyings(self.m, TODAY)}
        self.assertEqual(pick_expiry(self.m, u["NIFTY"], TODAY), date(2026, 10, 20))       # 5 days away
        self.assertEqual(pick_expiry(self.m, u["NIFTY"], date(2026, 10, 20)), date(2026, 10, 27))  # expiry day itself skipped
        self.assertEqual(pick_expiry(self.m, u["RELIANCE"], TODAY), date(2026, 10, 27))    # monthly (last of Oct)
        self.assertEqual(pick_expiry(self.m, u["RELIANCE"], date(2026, 10, 26)), date(2026, 11, 24))  # <2 DTE skipped

    def test_contract_resolution(self):
        c = self.m.option("NIFTY", date(2026, 10, 20), 25000, "CE")
        self.assertEqual(c.lot_size, 75)
        self.assertIsNone(self.m.option("NIFTY", date(2026, 10, 20), 99999, "CE"))


def chain(spot=25010.0, spread=0.5, oi=100000, vol=50000):
    rows = {}
    for k in range(-5, 6):
        s = 25000 + k * 50
        for t in ("CE", "PE"):
            intr = max(0, spot - s) if t == "CE" else max(0, s - spot)
            ltp = intr + 120
            d = 0.5 + (0.08 * -k if t == "CE" else 0.08 * k) * -1
            rows[(float(s), t)] = OptionQuote(float(s), t, key=f"k{s}{t}", ltp=ltp, bid=ltp - spread / 2, ask=ltp + spread / 2,
                                              oi=oi + (400000 if (t == "CE" and k == 3) else 0) + (400000 if (t == "PE" and k == -3) else 0),
                                              volume=vol, iv=14.0, delta=(0.5 - 0.07 * k) if t == "CE" else -(0.5 + 0.07 * k), theta=-15)
    snap = ChainSnapshot("NIFTY", date(2026, 10, 20), spot, 0.0, rows)
    snap.pcr = snap.calc_pcr()
    return snap


class TestPicker(unittest.TestCase):
    def setUp(self):
        self.m = Master(master_rows())
        self.u = [x for x in build_underlyings(self.m, TODAY) if x.name == "NIFTY"][0]
        self.now = datetime(2026, 10, 15, 10, 30, tzinfo=IST)

    def test_pick_call_put(self):
        c, q, why = pick_contract(self.m, self.u, "CALL", chain(), self.now)
        self.assertIsNotNone(c, why)
        self.assertEqual(c.opt_type, "CE")
        self.assertLessEqual(c.strike, 25050)
        c, q, why = pick_contract(self.m, self.u, "PUT", chain(), self.now)
        self.assertEqual(c.opt_type, "PE")

    def test_v7_does_not_reject_spread_cost_but_requires_executable_quote(self):
        c, q, why = pick_contract(self.m, self.u, "CALL", chain(spread=10), self.now)
        self.assertIsNotNone(c, why)  # no estimated spread-cost rejection gate
        ch = chain()
        for qt in ch.rows.values():
            qt.bid = qt.ask = 0.0
        c, q, why = pick_contract(self.m, self.u, "CALL", ch, self.now)
        self.assertIsNone(c)
        self.assertIn("bid/ask", why)
        c, q, why = pick_contract(self.m, self.u, "CALL", chain(oi=10, vol=10), self.now)
        self.assertIsNone(c)

    def test_iv_rank_block(self):
        c, q, why = pick_contract(self.m, self.u, "CALL", chain(), self.now, iv_rank=95)
        self.assertIsNone(c)
        self.assertIn("IV rank", why)

    def test_levels(self):
        lv = RiskRules.levels(200.0, 0.55, 40.0, 0.05, "CALL", 25010)
        self.assertLess(lv["sl"], 200)
        self.assertGreater(lv["t1"], 200)
        self.assertGreater(lv["t2"], lv["t1"])
        self.assertGreaterEqual(lv["sl"], 200 * (1 - cfg.MAX_SL_PCT) - 0.05)
        self.assertLess(lv["und_sl"], 25010)
        lv = RiskRules.levels(200.0, -0.55, 40.0, 0.05, "PUT", 25010)
        self.assertGreater(lv["und_sl"], 25010)
        self.assertLess(lv["und_t1"], 25010)


def make_signal(side="CALL"):
    return Signal(id="X-1", ts="2026-10-15T10:30:00+05:30", underlying="NIFTY", side=side, contract="NIFTY 25000 CE 20 OCT 26",
                  instrument_key="NSE_FO|abc", expiry="2026-10-20", strike=25000, opt_type="CE" if side == "CALL" else "PE",
                  lot_size=75, ltp=200, entry_lo=199, entry_hi=202, sl=160, t1=240, t2=290, spot=25010,
                  und_sl=24970 if side == "CALL" else 25050, cur_sl=160, prem_high=200, cost_lot=15000, max_loss_lot=3000,
                  rr=2.2, delta=0.55, theta=-15, iv=14, spread_pct=0.5, oi=1e5, volume=5e4, confidence=72, agree=3,
                  reasons=["EMA9 crossed above EMA21"], valid_till="10:45", exit_by="15:15", dte=5, provider="upstox")


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.db, self.tg = make_test_db(), FakeTG()
        self.t = Tracker(self.db, self.tg, None, None, {})
        self.now = datetime(2026, 10, 15, 11, 0, tzinfo=IST)

    def _add(self, sig):
        self.t.active[sig.id] = (sig, None)

    def test_t1_then_trail_then_t2(self):
        s = make_signal()
        self._add(s)
        self.t.evaluate(s, 241, None, self.now)
        self.assertEqual((s.status, s.cur_sl), ("T1_HIT", 200))
        self.t.evaluate(s, 270, None, self.now)
        self.assertEqual(s.cur_sl, 240)                       # trailed to T1
        self.t.evaluate(s, 291, None, self.now)
        self.assertEqual((s.status, s.result), ("CLOSED", "WIN"))
        gross = 91 * 75
        self.assertLess(s.pnl_lot, gross)                       # exit is filled on the bid side and net of costs
        self.assertGreater(s.pnl_lot, gross * 0.9)
        self.assertLess(s.exit_price, 291)
        self.assertNotIn(s.id, self.t.active)

    def test_stop_loss(self):
        s = make_signal()
        self._add(s)
        self.t.evaluate(s, 159, None, self.now)
        self.assertEqual((s.status, s.result), ("CLOSED", "LOSS"))

    def test_breakeven_after_t1(self):
        s = make_signal()
        self._add(s)
        self.t.evaluate(s, 245, None, self.now)
        self.t.evaluate(s, 199, None, self.now)
        self.assertEqual((s.status, s.result), ("CLOSED", "BREAKEVEN"))

    def test_underlying_stop_and_put_side(self):
        s = make_signal("PUT")
        self._add(s)
        self.t.evaluate(s, 190, 25051, self.now)
        self.assertEqual(s.exit_reason, "UND_SL")

    def test_time_exit(self):
        s = make_signal()
        self._add(s)
        self.t.evaluate(s, 210, None, datetime(2026, 10, 15, 15, 16, tzinfo=IST))
        self.assertEqual((s.status, s.result), ("CLOSED", "TIME_EXIT"))
        self.assertTrue(any("TIME" in m for m in self.tg.msgs))


class P:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.pings = name, fail, 0

    def option_chain(self, *a):
        if self.fail:
            raise ProviderError("down")
        return self.name

    def ping(self):
        return not self.fail


class TestFailover(unittest.TestCase):
    def test_switch_and_failback(self):
        up, dh, alerts = P("upstox"), P("dhan"), []
        fo = FailoverManager(up, dh, alert=alerts.append)
        self.assertEqual(fo.call("option_chain"), "upstox")
        up.fail = True
        for _ in range(cfg.FAIL_THRESHOLD):
            self.assertEqual(fo.call("option_chain"), "dhan")      # served by secondary even before the switch
        self.assertEqual(fo.active_name, "dhan")
        self.assertTrue(alerts)
        up.fail = False
        cfg.FAILBACK_PROBE_SEC = 0
        for _ in range(cfg.FAILBACK_OK_COUNT):
            fo.call("option_chain")
        self.assertEqual(fo.active_name, "upstox")


class TestParsers(unittest.TestCase):
    def test_upstox_chain(self):
        p = UpstoxProvider(token_fn=lambda: "t")
        p._get = lambda path, params=None: [{
            "strike_price": 25000, "underlying_spot_price": 25010, "pcr": 1.1,
            "call_options": {"instrument_key": "NSE_FO|1", "market_data": {"ltp": 120, "bid_price": 119.5, "ask_price": 120.5, "oi": 1000, "volume": 500},
                             "option_greeks": {"iv": 14, "delta": 0.55, "theta": -12}},
            "put_options": {"instrument_key": "NSE_FO|2", "market_data": {"ltp": 90, "bid_price": 89.5, "ask_price": 90.5, "oi": 1500, "volume": 400},
                            "option_greeks": {"iv": 15, "delta": -0.45, "theta": -11}}}]
        u = Underlying("NIFTY", "INDEX", "NSE_INDEX|Nifty 50")
        s = p.option_chain(u, date(2026, 10, 20))
        self.assertEqual(s.spot, 25010)
        self.assertAlmostEqual(s.pcr, 1.5)
        self.assertEqual(s.rows[(25000.0, "CE")].delta, 0.55)

    def test_dhan_chain(self):
        p = DhanProvider()
        p._post = lambda path, body, chain=False: {"data": {"last_price": 25010, "oc": {"25000.000000": {
            "ce": {"last_price": 120, "oi": 1000, "volume": 500, "implied_volatility": 14, "top_bid_price": 119.5, "top_ask_price": 120.5,
                   "greeks": {"delta": 0.55, "theta": -12}},
            "pe": {"last_price": 90, "oi": 1500, "volume": 400, "implied_volatility": 15, "top_bid_price": 89.5, "top_ask_price": 90.5,
                   "greeks": {"delta": -0.45, "theta": -11}}}}}}
        p.master.resolve = lambda u: setattr(u, "dhan_id", 13) or setattr(u, "dhan_seg", "IDX_I")
        u = Underlying("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", dhan_id=13, dhan_seg="IDX_I")
        s = p.option_chain(u, date(2026, 10, 20))
        self.assertEqual(s.rows[(25000.0, "PE")].oi, 1500)
        self.assertAlmostEqual(s.rows[(25000.0, "CE")].spread_pct, 1 / 120 * 100, places=2)


class TestDBAndFormat(unittest.TestCase):
    def test_roundtrip_and_format(self):
        db = make_test_db()
        s = make_signal()
        db.save_signal(s)
        self.assertEqual(len(db.active_signals("2026-10-15")), 1)
        s.status = "CLOSED"
        db.save_signal(s)
        self.assertEqual(len(db.active_signals("2026-10-15")), 0)
        db.save_oi("2026-10-15T10:00:00+05:30", "NIFTY", "2026-10-20", 25000, 1.1, 1, 1, 25200, 24800, 14)
        self.assertEqual(db.oi_before("NIFTY", "2026-10-20", "2026-10-15T10:30:00+05:30")["call_wall"], 25200)
        txt = format_signal(make_signal())
        self.assertIn("BUY CALL (CE)", txt)
        self.assertIn("Lot size", txt)
        self.assertIn("BUY PUT (PE)", format_signal(make_signal("PUT")))
        self.assertNotIn("LONG", txt.upper().replace("LONGER", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
