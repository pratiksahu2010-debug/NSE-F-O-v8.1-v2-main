import unittest
from strategies.mex_universe import MEX_SYMBOLS

class TestMexUniverse(unittest.TestCase):
    def test_exactly_100_unique_fixed_symbols(self):
        self.assertEqual(len(MEX_SYMBOLS), 100)
        self.assertEqual(len(set(MEX_SYMBOLS)), 100)
        self.assertTrue(all(x and x == x.upper() for x in MEX_SYMBOLS))

if __name__ == '__main__': unittest.main()
