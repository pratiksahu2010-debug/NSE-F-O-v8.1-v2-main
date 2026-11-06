from datetime import date, datetime, timezone

from models import ChainSnapshot, Contract, OptionQuote, Underlying
from signal_engine_v2.models import Candidate, CandleState, Side
from signal_engine_v2.runtime import build_option_signal


class FakeMaster:
    def __init__(self, contract):
        self.contract = contract

    def option(self, underlying, expiry, strike, opt_type):
        if (underlying, expiry, strike, opt_type) == (
                self.contract.underlying, self.contract.expiry,
                self.contract.strike, self.contract.opt_type):
            return self.contract
        return None


def test_runtime_selects_valid_contract_and_builds_signal():
    now = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    ts = datetime(2026, 11, 5, 9, 55, tzinfo=timezone.utc)
    expiry = date(2026, 11, 12)
    underlying = Underlying("NIFTY", "INDEX", "spot", lot_size=50)
    contract = Contract("NIFTY", expiry, 25000, "CE", "NSE_FO|NIFTY_CE",
                        "NIFTY26NOV25000CE", 50, .05)
    quote = OptionQuote(25000, "CE", ltp=150, bid=149, ask=151,
                        bid_qty=100, ask_qty=100, oi=10000, volume=2000,
                        iv=15, delta=.55, theta=-5, key="NSE_FO|NIFTY_CE")
    chain = ChainSnapshot("NIFTY", expiry, 25005, 1.1, {(25000, "CE"): quote}, now)
    candidate = Candidate("NIFTY", "INDEX", Side.CALL, "BIG_MOMENTUM", "runtime-build",
                          ts, CandleState.CLOSED, 85, 25000, 24950, 25090)
    signal, reason = build_option_signal(
        candidate, FakeMaster(contract), underlying, chain, now,
        atr_value=50, current_underlying_price=25005, provider="UPSTOX")
    assert reason == ""
    assert signal is not None
    assert signal.contract == "NIFTY26NOV25000CE"


def test_runtime_rejects_contract_master_instrument_key_mismatch():
    now = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    ts = datetime(2026, 11, 5, 9, 55, tzinfo=timezone.utc)
    expiry = date(2026, 11, 12)
    underlying = Underlying("NIFTY", "INDEX", "spot", lot_size=50)
    wrong_contract = Contract("NIFTY", expiry, 25000, "CE", "NSE_FO|WRONG",
                              "NIFTY26NOV25000CE", 50, .05)
    quote = OptionQuote(25000, "CE", ltp=150, bid=149, ask=151,
                        bid_qty=100, ask_qty=100, oi=10000, volume=2000,
                        iv=15, delta=.55, theta=-5, key="NSE_FO|NIFTY_CE")
    chain = ChainSnapshot("NIFTY", expiry, 25005, 1.1, {(25000, "CE"): quote}, now)
    candidate = Candidate("NIFTY", "INDEX", Side.CALL, "BIG_MOMENTUM", "runtime-key-mismatch",
                          ts, CandleState.CLOSED, 85, 25000, 24950, 25090)
    signal, reason = build_option_signal(
        candidate, FakeMaster(wrong_contract), underlying, chain, now,
        atr_value=50, current_underlying_price=25005, provider="UPSTOX")
    assert signal is None
    assert "instrument_key_mismatch" in reason
