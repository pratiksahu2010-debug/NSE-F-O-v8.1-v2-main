import threading
from unittest.mock import patch

from engine.scanner import Scanner


def test_v2_status_exposes_balanced_hybrid_gates_and_preview_count():
    scanner = Scanner.__new__(Scanner)
    scanner._stage1_diagnostics = {"v2_candidates": 3}
    scanner._v2_preview_lock = threading.Lock()
    scanner._v2_early_previews = {
        ("NIFTY", "2026-11-05T09:30:00+05:30"): {"side": "CALL"}
    }
    with patch("engine.scanner.cfg.V2_ENGINE_ENABLED", True):
        status = scanner._v2_status()
    assert status["enabled"] is True
    assert status["profile"] == "BALANCED_HYBRID"
    assert status["strategy_count"] == 9
    assert status["closed_bar_candidates"] == 3
    assert status["early_previews_cached"] == 1
    assert status["signal_only"] is True


def test_v2_history_backfill_respects_configured_minimum():
    scanner = Scanner.__new__(Scanner)
    with patch("engine.scanner.cfg.V2_ENGINE_ENABLED", True), \
         patch("engine.scanner.cfg.V2_MIN_HISTORY_BARS", 240):
        assert scanner._required_history_bars() == 240
