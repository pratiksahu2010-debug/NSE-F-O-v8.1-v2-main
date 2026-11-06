from datetime import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import Candle, Signal
from engine.scanner import _closed
from telegram_service import format_signal
from utils.market_hours import IST


def test_closed_candles_normalize_naive_provider_timestamps_to_ist():
    now = datetime(2026, 10, 28, 10, 10, tzinfo=IST)
    candles = [Candle(datetime(2026, 10, 28, 10, 0), 100, 102, 99, 101, 10)]
    result = _closed(candles, now)
    assert len(result) == 1
    assert result[0].ts.tzinfo is not None
    assert result[0].ts.utcoffset().total_seconds() == 19800


def test_signal_message_has_one_canonical_ist_timestamp():
    signal = Signal(ts="2026-10-28T10:01:02+05:30", underlying="NIFTY", side="CALL")
    message = format_signal(signal)
    assert message.count("Signal / alert time:") == 1
    assert "28-Oct-2026 10:01:02 IST" in message
    assert "Delivery time:" not in message
