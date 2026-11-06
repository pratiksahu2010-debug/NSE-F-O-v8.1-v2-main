from models import OptionQuote, Underlying
from signal_engine_v2.contracts import validate_option_quote


def underlying(kind="INDEX"):
    return Underlying("NIFTY" if kind == "INDEX" else "RELIANCE",
                      kind, "spot", lot_size=50)


def quote(**kwargs):
    data = dict(strike=25000, opt_type="CE", ltp=100, bid=99.5, ask=100.5,
                bid_qty=100, ask_qty=100, oi=10000, volume=2000,
                delta=.55, iv=15)
    data.update(kwargs)
    return OptionQuote(**data)


def test_valid_index_option_quote_passes():
    ok, reason = validate_option_quote(quote(), underlying())
    assert ok and reason == ""


def test_missing_delta_is_rejected_not_assumed():
    ok, reason = validate_option_quote(quote(delta=0), underlying())
    assert not ok
    assert reason == "greeks_delta_missing_or_invalid"


def test_wide_spread_is_rejected_even_if_legacy_spread_flag_is_off():
    ok, reason = validate_option_quote(quote(bid=90, ask=110), underlying())
    assert not ok
    assert reason.startswith("spread_too_wide")


def test_insufficient_depth_is_rejected():
    ok, reason = validate_option_quote(quote(bid_qty=10), underlying())
    assert not ok
    assert reason == "insufficient_bid_depth"


def test_stock_option_uses_stock_spread_limit():
    ok, reason = validate_option_quote(quote(bid=97, ask=103), underlying("STOCK"))
    assert not ok
    assert reason.startswith("spread_too_wide")
