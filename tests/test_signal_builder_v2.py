from datetime import date, datetime, timezone

from models import ChainSnapshot, Contract, OptionQuote
from signal_engine_v2.contracts import ContractSelection
from signal_engine_v2.models import Candidate, CandleState, Side
from signal_engine_v2.signal_builder import build_signal


def setup():
    now = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    ts = datetime(2026, 11, 5, 9, 55, tzinfo=timezone.utc)
    candidate = Candidate("NIFTY", "INDEX", Side.CALL, "BIG_MOMENTUM", "builder-test",
                          ts, CandleState.CLOSED, 85, 25000, 24950, 25090)
    contract = Contract("NIFTY", date(2026, 11, 12), 25000, "CE",
                        "NSE_FO|NIFTY_CE", "NIFTY26NOV25000CE", 50, .05)
    quote = OptionQuote(25000, "CE", ltp=150, bid=149, ask=151,
                        bid_qty=100, ask_qty=100, oi=10000, volume=2000,
                        iv=15, delta=.55, theta=-5)
    chain = ChainSnapshot("NIFTY", contract.expiry, 25005, 1.1)
    return candidate, ContractSelection(contract, quote), chain, now


def test_signal_builder_creates_persistable_confirmed_signal():
    candidate, selection, chain, now = setup()
    signal, reason = build_signal(candidate, selection, chain, now,
                                  atr_value=50, current_underlying_price=25005,
                                  provider="UPSTOX")
    assert reason == ""
    assert signal is not None
    assert signal.tier == "CONFIRMED"
    assert signal.strategy == "BIG_MOMENTUM"
    assert signal.confidence == 8.5
    assert signal.ltp == 151
    assert signal.max_loss_lot > 0


def test_signal_builder_rejects_chased_entry():
    candidate, selection, chain, now = setup()
    signal, reason = build_signal(candidate, selection, chain, now,
                                  atr_value=50, current_underlying_price=25100,
                                  provider="UPSTOX")
    assert signal is None
    assert reason in ("PRICE_EXTENDED_AFTER_CONFIRMATION",
                      "INSUFFICIENT_REMAINING_REWARD_RISK")
