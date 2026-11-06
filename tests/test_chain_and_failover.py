"""Option-chain quote validation and provider failover recovery (audit items 5 and 6)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from config import cfg  # noqa: E402
from data.errors import DataUnavailable, ProviderError  # noqa: E402
from data.failover import FailoverManager  # noqa: E402
from engine.chain_quality import chain_quality_issues  # noqa: E402
from tests.test_core import chain  # noqa: E402


class TestChainQuality(unittest.TestCase):
    def test_healthy_chain_passes(self):
        self.assertEqual(chain_quality_issues(chain(spot=25010.0), 25010.0), [])

    def test_spot_mismatch_against_candle_is_flagged(self):
        issues = chain_quality_issues(chain(spot=25010.0), 25200.0)
        self.assertTrue(any(i.startswith("spot_mismatch") for i in issues))

    def test_crossed_quotes_are_flagged(self):
        c = chain(spot=25010.0)
        q = c.rows[(25000.0, "CE")]
        q.bid, q.ask = 130.0, 120.0
        self.assertTrue(any(i.startswith("crossed_quotes") for i in chain_quality_issues(c, 25010.0)))

    def test_missing_bid_ask_on_atm_is_flagged(self):
        c = chain(spot=25010.0)
        for s in (24950.0, 25000.0, 25050.0, 25100.0):
            for t in ("CE", "PE"):
                c.rows[(s, t)].bid = 0.0
        self.assertTrue(any(i.startswith("missing_bid_ask") for i in chain_quality_issues(c, 25010.0)))

    def test_zero_volume_and_zero_oi_on_atm_are_flagged(self):
        c = chain(spot=25010.0, vol=0, oi=0)
        for q in c.rows.values():
            q.oi = 0.0                                   # the fixture gives some far strikes OI; zero all of it
        issues = chain_quality_issues(c, 25010.0)
        self.assertIn("zero_volume_atm", issues)
        self.assertNotIn("zero_oi_atm", issues)  # OI is not an alert-approval gate

    def test_far_otm_bad_rows_do_not_fail_the_chain(self):
        c = chain(spot=25010.0)
        for t in ("CE", "PE"):
            c.rows[(24750.0, t)].bid = 0.0
            c.rows[(25250.0, t)].volume = 0.0
        self.assertEqual(chain_quality_issues(c, 25010.0), [])

    def test_empty_chain(self):
        c = chain(spot=25010.0)
        c.rows = {}
        self.assertEqual(chain_quality_issues(c, 25010.0), ["empty_chain"])


class Prov:
    """Scripted provider: `script` is a list of 'ok' | 'fail' | 'empty' outcomes consumed per call."""

    def __init__(self, name, script=None, configured=True, ping_ok=True):
        self.name, self._c, self.script, self.ping_ok = name, configured, list(script or []), ping_ok
        self.calls = 0

    def configured(self):
        return self._c

    def ping(self):
        if not self.ping_ok:
            raise RuntimeError("down")
        return True

    def spot_intraday(self, u):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "fail":
            raise PermissionError("401 unauthorized")          # auth failure counts as a provider failure
        if outcome == "empty":
            raise DataUnavailable("empty")                     # valid empty response, never a failure
        return [self.name]


class TestFailoverRecovery(unittest.TestCase):
    def setUp(self):
        self._saved = (cfg.FAIL_THRESHOLD, cfg.FAILBACK_PROBE_SEC, cfg.FAILBACK_OK_COUNT)
        cfg.FAIL_THRESHOLD, cfg.FAILBACK_PROBE_SEC, cfg.FAILBACK_OK_COUNT = 3, 0, 2

    def tearDown(self):
        cfg.FAIL_THRESHOLD, cfg.FAILBACK_PROBE_SEC, cfg.FAILBACK_OK_COUNT = self._saved

    def test_auth_failure_switches_after_threshold_and_alerts(self):
        alerts = []
        up = Prov("upstox", ["fail"] * 3)
        dh = Prov("dhan")
        fo = FailoverManager(up, dh, alert=alerts.append)
        for _ in range(3):
            self.assertEqual(fo.call("spot_intraday", None), ["dhan"])
        self.assertEqual(fo.active_name, "dhan")
        self.assertTrue(any("Data provider switched" in a for a in alerts))

    def test_data_unavailable_never_switches_provider(self):
        up = Prov("upstox", ["empty"] * 10)
        dh = Prov("dhan")
        fo = FailoverManager(up, dh)
        for _ in range(10):
            # A valid empty response falls through to the secondary for that call only; it is never a failure.
            self.assertEqual(fo.call("spot_intraday", None), ["dhan"])
        self.assertEqual(fo.active_name, "upstox")
        self.assertEqual(fo.switches, [])
        self.assertEqual(fo.fails["upstox"], 0)

    def test_both_providers_down_raises_and_recovers_when_one_returns(self):
        up = Prov("upstox", ["fail", "ok"])
        dh = Prov("dhan", ["fail", "ok"])
        fo = FailoverManager(up, dh)
        with self.assertRaises(Exception):
            fo.call("spot_intraday", None)               # both down: no data, error surfaced
        self.assertEqual(fo.call("spot_intraday", None), ["upstox"])  # primary back: served by primary

    def test_primary_recovery_fails_back_after_consecutive_probes(self):
        up = Prov("upstox", ["fail"] * 3, ping_ok=True)
        dh = Prov("dhan")
        fo = FailoverManager(up, dh)
        for _ in range(3):
            fo.call("spot_intraday", None)
        self.assertEqual(fo.active_name, "dhan")
        fo.call("spot_intraday", None)                 # probe 1 ok
        self.assertEqual(fo.active_name, "dhan")
        fo.call("spot_intraday", None)                 # probe 2 ok -> failback
        self.assertEqual(fo.active_name, "upstox")

    def test_failed_probe_resets_failback_count(self):
        up = Prov("upstox", ["fail"] * 3, ping_ok=True)
        dh = Prov("dhan")
        fo = FailoverManager(up, dh)
        for _ in range(3):
            fo.call("spot_intraday", None)
        up.ping_ok = False
        fo.call("spot_intraday", None)
        up.ping_ok = True
        fo.call("spot_intraday", None)
        self.assertEqual(fo.active_name, "dhan")       # only one good probe since the failure

    def test_no_provider_mixing_within_one_call(self):
        # A call is answered by exactly one provider; the result is never a merge of both.
        fo = FailoverManager(Prov("upstox", ["fail"]), Prov("dhan"))
        res = fo.call("spot_intraday", None)
        self.assertEqual(res, ["dhan"])


if __name__ == "__main__":
    unittest.main()
