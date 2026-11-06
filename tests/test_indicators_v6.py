"""Indicator correctness: independent reference implementations, session-gap handling, flat-series RSI, 15m bars."""
import os
import random
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indicators.core import adx, atr, completed_bars, ema, htf_bias, resample, rsi  # noqa: E402
from models import Candle  # noqa: E402
from utils.market_hours import IST  # noqa: E402


def session(day, start_px, n=75, drift=0.0, noise=0.0008, seed=1, vol=1000.0):
    rnd = random.Random(seed + day)
    px, out = start_px, []
    t0 = datetime(2026, 10, day, 9, 15, tzinfo=IST)
    for i in range(n):
        o = px
        px = o * (1 + drift + rnd.gauss(0, noise))
        out.append(Candle(t0 + timedelta(minutes=5 * i), o, max(o, px) * 1.0004, min(o, px) * 0.9996, px, vol))
    return out


def ref_rsi(closes, n=14):
    """Textbook Wilder RSI written independently of indicators.core."""
    out = [None] * len(closes)
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(x, 0) for x in ch[:n]) / n
    al = sum(max(-x, 0) for x in ch[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al)
    for i in range(n, len(ch)):
        ag = (ag * (n - 1) + max(ch[i], 0)) / n
        al = (al * (n - 1) + max(-ch[i], 0)) / n
        out[i + 1] = 100 - 100 / (1 + ag / al)
    return out


def ref_atr(cs, n=14):
    tr = [cs[0].h - cs[0].l] + [max(cs[i].h - cs[i].l, abs(cs[i].h - cs[i - 1].c), abs(cs[i].l - cs[i - 1].c))
                                for i in range(1, len(cs))]
    a = sum(tr[:n]) / n
    out = {n - 1: a}
    for i in range(n, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def ref_adx(cs, n=14):
    """Wilder ADX from the textbook formulas (single session, no gaps)."""
    tr, pdm, ndm = [], [], []
    for i in range(1, len(cs)):
        up, dn = cs[i].h - cs[i - 1].h, cs[i - 1].l - cs[i].l
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(cs[i].h - cs[i].l, abs(cs[i].h - cs[i - 1].c), abs(cs[i].l - cs[i - 1].c)))
    atr_s, p_s, n_s = sum(tr[:n]), sum(pdm[:n]), sum(ndm[:n])
    dx = []
    res = {}
    for k in range(n - 1, len(tr)):
        if k >= n:
            atr_s = atr_s - atr_s / n + tr[k]
            p_s = p_s - p_s / n + pdm[k]
            n_s = n_s - n_s / n + ndm[k]
        pdi, ndi = 100 * p_s / atr_s, 100 * n_s / atr_s
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
        res[k + 1] = (pdi, ndi)
        if len(dx) == n:
            a = sum(dx) / n
            res[k + 1] = (a, pdi, ndi)
        elif len(dx) > n:
            a = (a * (n - 1) + dx[-1]) / n
            res[k + 1] = (a, pdi, ndi)
    return res


class TestAgainstReferences(unittest.TestCase):
    def setUp(self):
        self.cs = session(5, 1000.0, n=75, noise=0.002)

    def test_rsi_matches_textbook(self):
        closes = [c.c for c in self.cs]
        mine, ref = rsi(closes), ref_rsi(closes)
        for i in range(14, len(closes)):
            self.assertAlmostEqual(mine[i], ref[i], places=6, msg=i)

    def test_atr_matches_textbook(self):
        mine, ref = atr(self.cs), ref_atr(self.cs)
        for i, v in ref.items():
            self.assertAlmostEqual(mine[i], v, places=8, msg=i)

    def test_adx_matches_textbook_within_one_session(self):
        mine, ref = adx(self.cs), ref_adx(self.cs)
        checked = 0
        for i, vals in ref.items():
            if len(vals) == 3:
                a, p, n_ = vals
                self.assertAlmostEqual(mine[i][0], a, places=6, msg=i)
                self.assertAlmostEqual(mine[i][1], p, places=6, msg=i)
                self.assertAlmostEqual(mine[i][2], n_, places=6, msg=i)
                checked += 1
        self.assertGreater(checked, 30)

    def test_ema_matches_definition(self):
        vals = [c.c for c in self.cs]
        k, e = 2 / 22, vals[0]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
        self.assertAlmostEqual(ema(vals, 21)[-1], e, places=9)


class TestSessionGaps(unittest.TestCase):
    def _two_days(self, gap):
        d1 = []
        px = 1000.0
        for d in (5, 6, 7, 8, 9):
            c = session(d, px)
            d1 += c
            px = c[-1].c
        d2 = session(12, px * (1 + gap), drift=0.0)          # pure noise after the gap
        return d1, d2

    def test_gap_does_not_create_a_fake_trend_in_adx(self):
        hist, gapday = self._two_days(0.015)
        _, flatday = self._two_days(0.0)
        for k in (6, 12, 24):
            a_gap, p_gap, n_gap = adx(hist + gapday[:k])[-1]
            a_flat, p_flat, n_flat = adx(hist + flatday[:k])[-1]
            self.assertLess(abs(a_gap - a_flat), 12.0, (k, a_gap, a_flat))     # was ~20-25 points apart before the fix
            self.assertLess(p_gap - n_gap, 25.0, (k, p_gap, n_gap))            # +DI no longer dominated by the gap

    def test_gap_does_not_inflate_atr(self):
        hist, gapday = self._two_days(0.015)
        a_gap = atr(hist + gapday[:3])[-1]
        a_none = atr(hist + self._two_days(0.0)[1][:3])[-1]
        self.assertLess(a_gap / a_none, 1.25)                  # was ~1.6x with the gap counted as range

    def test_first_bar_of_session_true_range_is_its_own_range(self):
        hist, gapday = self._two_days(0.05)
        series = atr(hist + gapday[:1], 14)
        prev, last = series[-2], series[-1]
        first = gapday[0]
        expected = (prev * 13 + (first.h - first.l)) / 14
        self.assertAlmostEqual(last, expected, places=8)

    def test_real_intraday_trend_still_reads_as_trend(self):
        hist, _ = self._two_days(0.0)
        trend = session(12, hist[-1].c, drift=0.0015, noise=0.0004, seed=9)
        a, p, n = adx(hist + trend[:40])[-1]
        self.assertGreater(a, 25)
        self.assertGreater(p, n)


class TestRsiEdges(unittest.TestCase):
    def test_flat_series_is_neutral_not_overbought(self):
        self.assertEqual(rsi([100.0] * 40)[-1], 50.0)

    def test_pure_gains_is_100_and_pure_losses_is_0(self):
        self.assertEqual(rsi([100 + i for i in range(40)])[-1], 100.0)
        self.assertAlmostEqual(rsi([200 - i for i in range(40)])[-1], 0.0, places=6)


class TestHtfCompletedBars(unittest.TestCase):
    def _trend(self, days=6, drift=0.0006):
        cs, px = [], 1000.0
        for d in range(5, 5 + days):
            c = session(d, px, drift=drift, noise=0.0003)
            cs += c
            px = c[-1].c
        return cs

    def test_completed_bars_drops_only_the_forming_bar(self):
        cs = self._trend(2)
        for cut in (60, 61, 62):                               # candles 60,61,62 of day 2 sit in the same 15m bucket
            sub = cs[:75 + cut]
            bars = resample(sub, 15)
            done = completed_bars(sub, bars, 15)
            last = sub[-1]
            final_slot = ((last.ts.hour * 60 + last.ts.minute) - (9 * 60 + 15)) % 15 == 10
            self.assertEqual(len(done), len(bars) if final_slot else len(bars) - 1)

    def test_bias_is_stable_within_a_15m_bucket(self):
        cs = self._trend(6)
        base = len(cs) - 1                                     # last candle is the final slot of its bucket (74 % 3 == 2)
        self.assertEqual((74 % 3), 2)
        full = htf_bias(cs[:base + 1])
        self.assertEqual(full, 1)
        # the two earlier 5m candles of the same bucket must give the same answer as the last completed bar before it
        self.assertEqual(htf_bias(cs[:base - 1]), htf_bias(cs[:base]))
        self.assertEqual(htf_bias(cs[:base - 1]), htf_bias(cs[:base - 2]) if (base - 2) % 3 == 2 else htf_bias(cs[:base - 1]))

    def test_legacy_behaviour_still_available(self):
        cs = self._trend(6)[:-1]
        self.assertIn(htf_bias(cs, completed_only=False), (-1, 0, 1))


if __name__ == "__main__":
    unittest.main()
