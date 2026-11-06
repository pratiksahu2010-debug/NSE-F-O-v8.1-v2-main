"""tools/doctor.py must run end to end offline and report a healthy synthetic pipeline."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = ":memory:"

from tests.test_scanner_e2e import _FrozenDatetime  # noqa: E402
from tools import doctor  # noqa: E402


class TestDoctor(unittest.TestCase):
    def test_selftest_is_clean(self):
        lines = []
        with patch("engine.scanner.datetime", _FrozenDatetime):
            rep = doctor.run.__globals__["_selftest"](type("A", (), {})())
        self.assertEqual(rep.counts[doctor.FAIL], 0)
        self.assertGreaterEqual(rep.counts[doctor.OK], 6)

    def test_reports_failure_when_token_rejected(self):
        class Fo:
            def call(self, method, *a, **k):
                raise RuntimeError("upstox auth 401")
        lines = []
        rep = doctor.run(Fo(), None, [], out=lines.append, run_env=False, run_tg=False)
        self.assertGreaterEqual(rep.counts[doctor.FAIL], 1)
        self.assertTrue(any("rejected" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
