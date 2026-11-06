from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from models import Candle
from indicators.core import resample, completed_bars
from v7.sequential import SequentialEngine


IST = ZoneInfo("Asia/Kolkata")


def _bar(ts, price=100.0):
    return Candle(ts=ts, o=price, h=price + 1, l=price - 1, c=price + 0.2, v=1000, oi=0)


def test_completed_15m_requires_all_three_5m_constituents():
    start = datetime(2026, 10, 29, 9, 15, tzinfo=IST)
    candles = [_bar(start + timedelta(minutes=5 * i)) for i in range(3)]
    bars = resample(candles, 15)
    assert len(completed_bars(candles, bars, 15, 5)) == 1


def test_completed_15m_rejects_missing_middle_constituent():
    start = datetime(2026, 10, 29, 9, 15, tzinfo=IST)
    candles = [_bar(start), _bar(start + timedelta(minutes=10))]
    bars = resample(candles, 15)
    assert completed_bars(candles, bars, 15, 5) == []


def test_completed_15m_rejects_forming_bucket():
    start = datetime(2026, 10, 29, 9, 15, tzinfo=IST)
    candles = [_bar(start), _bar(start + timedelta(minutes=5))]
    bars = resample(candles, 15)
    assert completed_bars(candles, bars, 15, 5) == []


def test_v7_sequential_waits_when_recent_intraday_history_has_gap():
    start = datetime(2026, 10, 29, 9, 15, tzinfo=IST)
    candles = [_bar(start + timedelta(minutes=5 * i)) for i in range(61)]
    # Remove one constituent in the recent same-session window.
    candles.pop(-3)
    now = candles[-1].ts + timedelta(minutes=5, seconds=10)
    result = SequentialEngine(adx_min=25).evaluate(candles, now=now)
    assert result.status == "WAITING"
    assert "gap" in result.reason
