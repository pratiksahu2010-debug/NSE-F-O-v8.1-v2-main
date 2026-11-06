import unittest

from strategies.normal_universe import NORMAL_SYMBOLS, NORMAL_SYMBOL_SET
from strategies.mex_universe import MEX_SYMBOLS


class TestNormalUniverse(unittest.TestCase):
    def test_exactly_50_unique_fixed_symbols(self):
        self.assertEqual(len(NORMAL_SYMBOLS), 50)
        self.assertEqual(len(NORMAL_SYMBOL_SET), 50)
        self.assertTrue(all(x and x == x.upper() for x in NORMAL_SYMBOLS))

    def test_normal_universe_is_covered_by_momentum_universe(self):
        self.assertTrue(NORMAL_SYMBOL_SET.issubset(set(MEX_SYMBOLS)))


if __name__ == "__main__":
    unittest.main()
