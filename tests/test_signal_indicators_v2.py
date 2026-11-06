from datetime import datetime, timedelta, timezone

from signal_engine_v2.indicators import calculate
from signal_engine_v2.models import Candle


def make_bars(n=260):
    start = datetime(2026, 10, 1, 9, 15, tzinfo=timezone.utc)
    bars = []
    price = 100.0
    for i in range(n):
        price += 0.12 if i % 7 else -0.04
        bars.append(Candle(start + timedelta(minutes=5 * i), price - 0.05,
                           price + 0.20, price - 0.20, price, 1000 + i * 2, True))
    return bars


def test_indicator_snapshot_has_warmed_trend_and_momentum_values():
    values = calculate(make_bars())
    assert values["ema_9"] is not None
    assert values["ema_21"] is not None
    assert values["ema_200"] is not None
    assert values["rsi_14"] is not None
    assert values["adx_14"] is not None
    assert values["atr_14"] is not None
    assert values["macd_hist"] is not None


def test_donchian_excludes_current_bar():
    bars = make_bars(30)
    values = calculate(bars)
    assert values["donchian_high_20"] == max(x.high for x in bars[-21:-1])
    assert values["donchian_low_20"] == min(x.low for x in bars[-21:-1])


def test_missing_volume_does_not_fabricate_rvol_or_mfi():
    bars = [Candle(x.timestamp, x.open, x.high, x.low, x.close, 0, True) for x in make_bars()]
    values = calculate(bars)
    assert values["rvol_20"] is None
    assert values["mfi_14"] is None
    assert values["obv"] is None


def test_rvol_20_requires_full_21_candle_window():
    values = calculate(make_bars(10))
    assert values["rvol_20"] is None
