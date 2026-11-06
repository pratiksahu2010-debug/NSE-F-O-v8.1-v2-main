import unittest
from strategies.mex_scanner import MomentumExplosionScanner
from strategies.mex_universe import MEX_SYMBOLS
from tests.test_core import trend_scenario


class MemoryDB:
    def __init__(self): self.values = {}
    def kv_get(self, key): return self.values.get(key)
    def kv_set(self, key, value): self.values[key] = value


class MexScannerEngineTests(unittest.TestCase):
    def test_non_universe_symbol_is_never_evaluated(self):
        engine = MomentumExplosionScanner(MemoryDB())
        history, session = trend_scenario(1)
        result = engine.scan("NOT_A_MEX_SYMBOL", session, history + session)
        self.assertFalse(result["eligible"])
        self.assertEqual(engine.snapshot()["eligible"], 0)

    def test_no_trigger_exposes_directional_gate_failures(self):
        from unittest.mock import patch
        from config import cfg
        engine = MomentumExplosionScanner(MemoryDB())
        history, session = trend_scenario(1)
        failures = {"fresh_boundary_break": False, "strong_close": True}
        rejected = {"gate_failures": ["fresh_boundary_break"], "gate_results": failures,
                    "impulse_atr": 0.1}
        with patch("strategies.mex_scanner.momentum_expansion_index",
                   side_effect=[(0.0, "MEX blocked: fresh_boundary_break", rejected),
                                (0.0, "MEX blocked: fresh_boundary_break", rejected)]):
            engine.begin_cycle()
            result = engine.scan(MEX_SYMBOLS[0], session, history + session)
        self.assertEqual(result["metrics"]["rejected_directions"]["CALL"]["gate_failures"],
                         ["fresh_boundary_break"])
        self.assertEqual(result["metrics"]["rejected_directions"]["PUT"]["gate_results"], failures)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["no_trigger"], 1)
        self.assertEqual(snapshot["gate_failures"]["fresh_boundary_break"], 2)

    def test_engine_owns_fixed_universe_without_lifecycle(self):
        engine = MomentumExplosionScanner(MemoryDB())
        self.assertEqual(len(engine.universe), 100)
        self.assertEqual(engine.universe, frozenset(MEX_SYMBOLS))
        engine.begin_cycle()
        history, session = trend_scenario(1)
        result = engine.scan(MEX_SYMBOLS[0], session, history + session)
        self.assertTrue(result["eligible"])
        self.assertIn(result["state"], {"IDLE", "CONFIRMED"})
        self.assertEqual(engine.snapshot()["pending"], 0)


if __name__ == "__main__":
    unittest.main()
