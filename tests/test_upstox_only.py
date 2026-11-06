import unittest
from data.upstox_only import UpstoxOnlyProvider


class FakeUpstox:
    def __init__(self):
        self.token_fn = lambda: "token"
        self.value = 7
    def ping(self):
        return True
    def daily(self, *args, **kwargs):
        return self.value


class UpstoxOnlyTests(unittest.TestCase):
    def test_adapter_calls_only_upstox_and_reports_metrics(self):
        provider = UpstoxOnlyProvider(FakeUpstox())
        self.assertEqual(provider.call("daily", object()), 7)
        self.assertEqual(provider.active_name, "upstox")
        health = provider.health()
        self.assertEqual(health["mode"], "upstox_only_no_failover")
        self.assertEqual(health["calls"]["daily"], 1)
        self.assertGreaterEqual(health["latency_ms"]["daily"], 0)

    def test_provider_errors_are_not_hidden_by_failover(self):
        class Broken(FakeUpstox):
            def daily(self, *args, **kwargs):
                raise RuntimeError("offline")
        provider = UpstoxOnlyProvider(Broken())
        with self.assertRaisesRegex(RuntimeError, "offline"):
            provider.call("daily", object())
        health = provider.health()
        self.assertEqual(health["failures"]["daily"], 1)
        self.assertIn("offline", health["last_error"])


if __name__ == "__main__":
    unittest.main()
