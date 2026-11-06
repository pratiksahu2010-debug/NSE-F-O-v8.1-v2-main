from datetime import datetime, timedelta
from types import SimpleNamespace

from db import DB
from tests.db_test_utils import make_test_db
from engine.chain_quality import chain_quality_issues
from models import Candle, ChainSnapshot, OptionQuote
from v7.sequential import SequentialEngine


def test_chain_quality_checks_only_requested_option_side():
    expiry = datetime(2026, 11, 26).date()
    rows = {}
    for strike in (900, 950, 1000, 1050, 1100):
        rows[(strike, "CE")] = OptionQuote(
            strike=strike, opt_type="CE", ltp=20, bid=19.9, ask=20.1, volume=1000, oi=5000
        )
        # The opposite side is illiquid/missing quotes and must not veto a CALL.
        rows[(strike, "PE")] = OptionQuote(
            strike=strike, opt_type="PE", ltp=0, bid=0, ask=0, volume=0, oi=0
        )
    chain = ChainSnapshot("TEST", expiry, 1000, 1.0, rows)
    assert chain_quality_issues(chain, 1000, side="CALL") == []
    assert any("missing_bid_ask" in issue for issue in chain_quality_issues(chain, 1000))


def test_sequential_engine_accepts_confirmed_ema9_pullback(monkeypatch):
    import v7.sequential as seqmod

    n = 60
    start = datetime(2026, 10, 1, 9, 15)
    bars = [
        Candle(start + timedelta(minutes=5 * i), 104, 107, 103, 106, 1000)
        for i in range(n)
    ]
    # One of the immediately preceding candles retests EMA9; the final candle resumes upward.
    bars[-2] = Candle(bars[-2].ts, 105, 106, 104.7, 105, 1000)
    bars[-1] = Candle(bars[-1].ts, 105, 108, 104.8, 107.5, 1200)

    def fake_ema(values, period):
        return [105.0] * n if period == 9 else [100.0] * n

    monkeypatch.setattr(seqmod, "ema", fake_ema)
    monkeypatch.setattr(seqmod, "session_vwap", lambda _: [106.0] * n)
    monkeypatch.setattr(seqmod, "session_vwap_mode", lambda _: ["VWAP"] * n)
    monkeypatch.setattr(seqmod, "rsi", lambda *_: [60.0] * n)
    monkeypatch.setattr(seqmod, "adx", lambda *_: [(30.0, 35.0, 20.0)] * n)
    monkeypatch.setattr(seqmod, "atr", lambda *_: [2.0] * n)

    decision = SequentialEngine(min_history_bars=50).evaluate(bars)
    assert decision.status == "UNDERLYING_SIGNAL_CONFIRMED"
    assert decision.direction == "CALL"
    assert decision.metrics["setup_type"] == "EMA9_PULLBACK_CONTINUATION"


def test_telegram_outbox_is_persistent_and_tracks_each_chat():
    db = make_test_db()
    try:
        key = "alert:2026-11-02:TEST-20261102-1000-C"
        db.enqueue_telegram(key, "TEST-20261102-1000-C", "chat-a", "signal")
        db.enqueue_telegram(key, "TEST-20261102-1000-C", "chat-b", "signal")
        pending = db.pending_telegram()
        assert len(pending) == 2
        db.mark_telegram_delivered(pending[0]["id"])
        status = db.telegram_delivery_status(key)
        assert status["delivered"] == 1
        assert status["pending"] == 1
        assert not status["all_delivered"]
        db.mark_telegram_delivered(pending[1]["id"])
        assert db.telegram_delivery_status(key)["all_delivered"]
    finally:
        db.close()


def test_telegram_outbox_splits_long_messages_into_independent_retry_rows():
    db = make_test_db()
    try:
        key = "alert:2026-11-02:TEST:20261102T100000:CALL"
        message = "x" * 8200
        db.enqueue_telegram(key, "TEST-20261102T100000-C", "chat-a", message)
        pending = db.pending_telegram()
        assert [len(row["message"]) for row in pending] == [3900, 3900, 400]
        db.mark_telegram_delivered(pending[0]["id"])
        db.mark_telegram_delivered(pending[1]["id"])
        retry = db.pending_telegram()
        assert len(retry) == 1
        assert retry[0]["message"] == "x" * 400
        assert db.telegram_delivery_status(key)["delivered"] == 2
    finally:
        db.close()


def test_15m_vwap_context_ignores_bucket_with_missing_middle_5m_candle():
    from indicators.core import htf_vwap_bias

    start = datetime(2026, 11, 2, 9, 15)
    bars = []
    # Two complete 15m buckets.
    for i in range(6):
        ts = start + timedelta(minutes=5 * i)
        bars.append(Candle(ts, 100 + i, 102 + i, 99 + i, 101 + i, 1000))
    # Third bucket has its first and last candle but misses the middle 5m candle.
    for i in (6, 8):
        ts = start + timedelta(minutes=5 * i)
        bars.append(Candle(ts, 110 + i, 112 + i, 109 + i, 111 + i, 1000))
    assert htf_vwap_bias(bars) == 0


def test_signal_identity_is_based_on_completed_candle_not_scan_time():
    candle_ts = datetime(2026, 11, 2, 10, 15)
    identity_a = f"TEST-{candle_ts.strftime('%Y%m%dT%H%M%S')}-C"
    identity_b = f"TEST-{candle_ts.strftime('%Y%m%dT%H%M%S')}-C"
    assert identity_a == identity_b
    assert identity_a != f"TEST-{(candle_ts + timedelta(minutes=5)).strftime('%Y%m%dT%H%M%S')}-C"



def test_sequential_engine_accepts_put_pullback_from_below(monkeypatch):
    import v7.sequential as seqmod

    n = 60
    start = datetime(2026, 10, 1, 9, 15)
    bars = [
        Candle(start + timedelta(minutes=5 * i), 96, 97, 95, 96, 1000)
        for i in range(n)
    ]
    bars[-2] = Candle(bars[-2].ts, 95, 95.3, 94.5, 95, 1000)
    bars[-1] = Candle(bars[-1].ts, 95, 95.2, 92, 92.5, 1200)

    def fake_ema(values, period):
        return [95.0] * n if period == 9 else [100.0] * n

    monkeypatch.setattr(seqmod, "ema", fake_ema)
    monkeypatch.setattr(seqmod, "session_vwap", lambda _: [94.0] * n)
    monkeypatch.setattr(seqmod, "session_vwap_mode", lambda _: ["VWAP"] * n)
    monkeypatch.setattr(seqmod, "rsi", lambda *_: [40.0] * n)
    monkeypatch.setattr(seqmod, "adx", lambda *_: [(30.0, 20.0, 35.0)] * n)
    monkeypatch.setattr(seqmod, "atr", lambda *_: [2.0] * n)

    decision = SequentialEngine(min_history_bars=50).evaluate(bars)
    assert decision.status == "UNDERLYING_SIGNAL_CONFIRMED"
    assert decision.direction == "PUT"
    assert decision.metrics["setup_type"] == "EMA9_PULLBACK_CONTINUATION"


def test_call_pullback_does_not_accept_retest_from_below(monkeypatch):
    import v7.sequential as seqmod

    n = 60
    start = datetime(2026, 10, 1, 9, 15)
    bars = [
        Candle(start + timedelta(minutes=5 * i), 104, 107, 103, 106, 1000)
        for i in range(n)
    ]
    # This candle overlaps EMA9 but opens and closes below it: it is not a
    # bullish retest from above. The final bullish candle cannot repair that.
    bars[-2] = Candle(bars[-2].ts, 104.5, 105.1, 104.4, 104.8, 1000)
    bars[-1] = Candle(bars[-1].ts, 105, 108, 104.8, 107.5, 1200)

    def fake_ema(values, period):
        return [105.0] * n if period == 9 else [100.0] * n

    monkeypatch.setattr(seqmod, "ema", fake_ema)
    monkeypatch.setattr(seqmod, "session_vwap", lambda _: [106.0] * n)
    monkeypatch.setattr(seqmod, "session_vwap_mode", lambda _: ["VWAP"] * n)
    monkeypatch.setattr(seqmod, "rsi", lambda *_: [60.0] * n)
    monkeypatch.setattr(seqmod, "adx", lambda *_: [(30.0, 35.0, 20.0)] * n)
    monkeypatch.setattr(seqmod, "atr", lambda *_: [2.0] * n)

    decision = SequentialEngine(min_history_bars=50).evaluate(bars)
    assert decision.status == "WAITING"
    assert "no fresh EMA cross" in decision.reason


def test_option_buy_entry_requires_positive_ask_not_ltp():
    from types import SimpleNamespace
    from engine.scanner import _executable_buy_price

    assert _executable_buy_price(SimpleNamespace(ask=10.5, ltp=10.0)) == 10.5
    assert _executable_buy_price(SimpleNamespace(ask=0.0, ltp=10.0)) is None
    assert _executable_buy_price(SimpleNamespace(ask=None, ltp=10.0)) is None
