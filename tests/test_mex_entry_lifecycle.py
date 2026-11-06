"""Regression tests for same-candle MEX confirmation without dormant lifecycle state."""
from datetime import datetime
from unittest.mock import patch

from config import cfg
from models import Candle
from strategies.mex_scanner import MomentumExplosionScanner
from utils.market_hours import IST


def _bar(close=102.0):
    return Candle(datetime(2026, 11, 2, 10, 0, tzinfo=IST), 100, max(103, close), 99, close, 1000)


def test_mex_detection_is_confirmed_on_its_detection_candle():
    scanner = MomentumExplosionScanner()
    symbol = next(iter(scanner.universe))
    bar = _bar()
    with patch.object(cfg, "MEX_ENABLED", True), patch(
        "strategies.mex_scanner.momentum_expansion_index",
        side_effect=[(85.0, "bullish impulse", {"range": 1.8}),
                     (0.0, "no bearish setup", {})],
    ):
        result = scanner.scan(symbol, [bar], [bar])
    assert result["state"] == "CONFIRMED"
    assert result["live"]["state"] == "CONFIRMED"
    assert result["sgn"] == 1
    assert scanner.snapshot()["confirmed"] == 1
    assert scanner.snapshot()["pending"] == 0


def test_mex_no_trigger_stays_idle_without_pending_lifecycle():
    scanner = MomentumExplosionScanner()
    symbol = next(iter(scanner.universe))
    bar = _bar(100.0)
    with patch.object(cfg, "MEX_ENABLED", True), patch(
        "strategies.mex_scanner.momentum_expansion_index",
        return_value=(0.0, "no setup", {}),
    ):
        result = scanner.scan(symbol, [bar], [bar])
    assert result["state"] == "IDLE"
    assert result["live"]["state"] == "IDLE"
    assert scanner.snapshot()["pending"] == 0
