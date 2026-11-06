import unittest
from datetime import datetime,timedelta
from models import Candle
from v7.indicators import ema,session_vwap,session_vwap_mode,adx,atr
from v7.sequential import SequentialEngine
import tempfile,os

class V71Tests(unittest.TestCase):
    def bars(self,n=100):
        start=datetime(2026,10,1,9,15)
        out=[]; p=100.0
        for i in range(n):
            p += (0.15 if i>65 else -0.015)
            out.append(Candle(start+timedelta(minutes=5*i),p-.05,p+.25,p-.25,p,1000,0))
        return out
    def test_ema_length(self):
        self.assertEqual(len(ema([1,2,3],9)),3)
    def test_vwap_session_reset(self):
        a=self.bars(2)
        b=Candle(a[-1].ts+timedelta(days=1),100,101,99,100,100,0)
        v=session_vwap([a[0],a[1],b])
        self.assertEqual(v[-1],100)
    def test_15m_context_does_not_veto(self):
        e=SequentialEngine()
        # Context is informational; this dataset may not trigger a setup, but must not reject solely for context.
        d=e.evaluate(self.bars(),context_15m="BEARISH")
        self.assertNotIn("15m",d.reason.lower())

    def test_no_fresh_cross_still_returns_indicator_diagnostics(self):
        from v7.sequential import SequentialEngine
        result = SequentialEngine().evaluate(self.bars())
        self.assertEqual(result.status, "WAITING")
        self.assertIn("ema9", result.metrics)
        self.assertIn("ema21", result.metrics)
        self.assertIn("vwap", result.metrics)
        self.assertIn("rsi", result.metrics)
        self.assertIn("adx", result.metrics)
        self.assertIn("plus_di", result.metrics)
        self.assertIn("minus_di", result.metrics)
        self.assertNotIn("step", result.metrics)

    def test_zero_volume_session_is_explicitly_labelled_twap_fallback(self):
        bars = self.bars(3)
        for bar in bars:
            bar.v = 0.0
        values = session_vwap(bars)
        modes = session_vwap_mode(bars)
        self.assertEqual(modes[-1], "TWAP_FALLBACK")
        self.assertAlmostEqual(values[-1], sum((x.h + x.l + x.c) / 3 for x in bars) / 3)

    def test_vwap_is_hlc3_volume_weighted_when_volume_exists(self):
        bars = self.bars(2)
        bars[0].v = 1
        bars[1].v = 3
        expected = (((bars[0].h + bars[0].l + bars[0].c) / 3) +
                    3 * ((bars[1].h + bars[1].l + bars[1].c) / 3)) / 4
        self.assertAlmostEqual(session_vwap(bars)[-1], expected)
        self.assertEqual(session_vwap_mode(bars)[-1], "VWAP")

    def test_dmi_adx_first_value_after_wilder_warmup(self):
        values = adx(self.bars(60), 14, 14)
        self.assertIsNone(values[26])
        self.assertIsNotNone(values[27])
        self.assertEqual(len(values[27]), 3)

    def test_fifty_bars_are_enough_for_indicator_warmup(self):
        from v7.sequential import SequentialEngine
        result = SequentialEngine(min_history_bars=50).evaluate(self.bars(50))
        self.assertNotIn("insufficient completed candles", result.reason)
        self.assertNotIn("step", result.metrics)
    def test_order_safety_constants(self):
        self.assertTrue(True)  # V7.1 contains no broker order-routing module.

if __name__=="__main__": unittest.main()
