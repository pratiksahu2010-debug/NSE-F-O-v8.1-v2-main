"""The configured stock universe is fixed and de-duplicated at bootstrap."""
from strategies.mex_universe import MEX_SYMBOLS
from strategies.normal_universe import NORMAL_SYMBOLS
from strategies.orb_universe import ORB_SYMBOLS


def test_fixed_universe_is_deduplicated_and_normal_is_subset():
    union = list(dict.fromkeys((*NORMAL_SYMBOLS, *MEX_SYMBOLS, *ORB_SYMBOLS)))
    assert len(MEX_SYMBOLS) == 100
    assert len(ORB_SYMBOLS) == 100
    assert len(union) == 100
    assert set(NORMAL_SYMBOLS).issubset(set(union))


def test_all_strategy_universes_use_valid_symbol_strings():
    for symbol in (*NORMAL_SYMBOLS, *MEX_SYMBOLS, *ORB_SYMBOLS):
        assert symbol
        assert symbol == symbol.upper()
        assert " " not in symbol
