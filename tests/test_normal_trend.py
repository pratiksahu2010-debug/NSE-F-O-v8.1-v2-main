from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from models import Candle
from strategies.normal_trend import (
    evaluate_normal_trend,
    evaluate_normal_trend_intrabar,
)

IST = ZoneInfo("Asia/Kolkata")


def _candles(current, baseline_low=100.5):
    start = datetime(2026, 11, 3, 9, 15, tzinfo=IST)
    rows = [
        Candle(start + timedelta(minutes=5 * i), 101.0, 102.0, baseline_low, 101.0, 1000.0)
        for i in range(44)
    ]
    rows.append(current)
    return rows


def _patched_indicators():
    n = 45
    ema_fast = [100.5] * (n - 1) + [101.0]
    ema_slow = [100.0] * n
    macd_line = [0.2] * (n - 1) + [0.3]
    macd_signal = [0.0] * n
    histogram = [0.1] * (n - 1) + [0.2]
    rsi_values = [48.5] * (n - 1) + [49.0]
    adx_values = [(21.0, 25.0, 15.0)] * (n - 1) + [(22.0, 25.0, 15.0)]
    return (
        patch("strategies.normal_trend.ema", side_effect=lambda values, period: ema_fast if period == 9 else ema_slow),
        patch("strategies.normal_trend.macd", return_value=(macd_line, macd_signal, histogram)),
        patch("strategies.normal_trend.rsi", return_value=rsi_values),
        patch("strategies.normal_trend.adx", return_value=adx_values),
        patch("strategies.normal_trend.vwap", return_value=[99.0] * 5),
        patch("strategies.normal_trend.atr", return_value=[1.0] * n),
    )


def _evaluate(current, intrabar=False, baseline_low=100.5):
    candles = _candles(current, baseline_low)
    session = candles[-5:]
    patches = _patched_indicators()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        if intrabar:
            return evaluate_normal_trend_intrabar(session[:-1], candles[:-1], current)
        return evaluate_normal_trend(session, candles)


def test_early_trend_detected_before_adx_reaches_25():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     101.3, 101.6, 101.3, 101.5, 1500.0)
    result = _evaluate(current, baseline_low=101.3)
    assert result["side"] == 1
    assert result["setup_type"] == "EARLY_TREND"
    assert result["metrics"]["adx"] == 22.0


def test_trend_pullback_resumption_is_a_separate_setup():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     100.9, 101.4, 100.9, 101.3, 1500.0)
    result = _evaluate(current)
    assert result["side"] == 1
    assert result["setup_type"] == "TREND_PULLBACK"


def test_fresh_breakout_is_detected_before_late_extension():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     101.5, 102.5, 101.4, 102.4, 1500.0)
    result = _evaluate(current)
    assert result["side"] == 1
    assert result["setup_type"] == "MOMENTUM_BREAKOUT"


def test_adx_not_rising_below_established_floor_is_rejected_with_gate_reason():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     100.9, 101.6, 100.8, 101.5, 1500.0)
    candles = _candles(current)
    session = candles[-5:]
    patches = list(_patched_indicators())
    patches[3] = patch(
        "strategies.normal_trend.adx",
        return_value=[(22.0, 25.0, 15.0)] * 44 + [(21.0, 25.0, 15.0)],
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = evaluate_normal_trend(session, candles)
    assert result["side"] == 0
    assert "CALL:adx_strength" in result["gate_failures"]
    assert "gate_results" in result


def test_live_intrabar_uses_the_same_early_trend_setup_rules():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     101.3, 101.6, 101.3, 101.5, 1500.0)
    result = _evaluate(current, intrabar=True, baseline_low=101.3)
    assert result["side"] == 1
    assert result["setup_type"] == "EARLY_TREND"



def test_breakout_that_is_already_extended_from_trigger_is_rejected():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     101.5, 104.5, 101.4, 104.2, 1500.0)
    result = _evaluate(current)
    assert result["side"] == 0
    assert "CALL:entry_extension" in result["gate_failures"]
    assert result["metrics"]["entry_extension_atr"] > 1.50


def test_macd_is_not_a_hard_normal_gate():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     101.5, 102.5, 101.4, 102.4, 1500.0)
    candles = _candles(current)
    session = candles[-5:]
    patches = list(_patched_indicators())
    patches[1] = patch("strategies.normal_trend.macd", return_value=([0.0] * 45, [0.0] * 45, [-2.0, -3.0]))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = evaluate_normal_trend(session, candles)
    assert result["side"] == 1
    assert result["gate_results"]["CALL"]["macd_supporting"] is False


def test_normal_signal_does_not_require_macd_series():
    current = Candle(datetime(2026, 11, 3, 12, 55, tzinfo=IST),
                     101.5, 102.5, 101.4, 102.4, 1500.0)
    candles = _candles(current)
    session = candles[-5:]
    patches = list(_patched_indicators())
    patches[1] = patch("strategies.normal_trend.macd", return_value=([], [], []))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = evaluate_normal_trend(session, candles)
    assert result["side"] == 1
