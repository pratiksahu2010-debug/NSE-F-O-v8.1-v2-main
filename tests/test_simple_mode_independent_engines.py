from datetime import datetime, timedelta
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engine import scanner as scanner_module
from models import Candle, Underlying, Vote
from utils.market_hours import IST


def _stage1_scanner(symbol, simple_candidate):
    scanner = scanner_module.Scanner.__new__(scanner_module.Scanner)
    scanner.unds = [symbol]
    scanner.pool = Mock()
    scanner.ban = set()
    scanner.hist = {}
    scanner.live_context = {}
    scanner.mkt_bias = 0
    scanner.mkt_ret = None
    scanner._why = {}
    scanner._rej = {}
    scanner._symbol_rejections = {}
    scanner.v7_diagnostics = {}
    scanner.candidate_watchlist = {}
    scanner._candidate_lock = threading.RLock()
    scanner._ready_lock = threading.Lock()
    scanner._ready_state = {}
    scanner._backfill_lock = threading.Lock()
    scanner._backfill_this_scan = 0
    scanner._archive = Mock()
    scanner._watch = Mock()
    scanner._schedule_candidate_watchlist_persistence = Mock()
    scanner._simple_candidate = Mock(return_value=simple_candidate)
    scanner.mex = Mock()
    scanner.mex.universe = {symbol.name}
    scanner.mex.scan.return_value = {
        "sgn": None, "score": 0, "state": "IDLE", "live": {}, "metrics": {}
    }
    scanner.momentum_orb = Mock()
    scanner.momentum_orb.universe = {symbol.name}
    scanner.momentum_orb.scan.return_value = {
        "confirmed_now": False, "metrics": {}
    }
    scanner.live_engine = Mock()
    return scanner


def test_simple_mode_runs_mex_and_orb_without_changing_simple_normal_route():
    now = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    candle = Candle(now - timedelta(minutes=5), 100, 102, 99, 101, 1000, 0)
    symbol = Underlying("RELIANCE", "STOCK", "NSE_EQ|RELIANCE")
    simple_candidate = {
        "ps": 1.0,
        "votes": [Vote("SIMPLE_TREND", 1.0, "existing SIMPLE rule")],
        "entry_candidate": {"strategy": "SIMPLE_TREND"},
    }
    scanner = _stage1_scanner(symbol, simple_candidate)

    with patch.object(scanner_module.cfg, "STRATEGY_MODE", "SIMPLE"), \
         patch.object(scanner_module.events, "symbol_event", return_value=False), \
         patch.object(scanner_module, "_stage1_fetch_results",
                      return_value=iter([(symbol, ([candle], None), None)])):
        candidates, hits = scanner.stage1(now)

    scanner._simple_candidate.assert_called_once()
    scanner.mex.scan.assert_called_once_with(symbol.name, [candle], [candle])
    scanner.momentum_orb.scan.assert_called_once_with(symbol.name, [candle], [candle])
    assert hits == 1
    assert len(candidates) == 1
    assert candidates[0]["selected_engine"] == "SIMPLE_TREND"
    assert candidates[0]["votes"][0].name == "SIMPLE_TREND"
    assert candidates[0]["entry_candidate"]["strategy"] == "SIMPLE_TREND"


def test_simple_mode_live_dispatch_respects_normal_universe_and_keeps_mex_orb():
    scanner = scanner_module.Scanner.__new__(scanner_module.Scanner)
    symbol = Underlying("ABC", "STOCK", "NSE_EQ|ABC")
    normal_symbol = Underlying("RELIANCE", "STOCK", "NSE_EQ|RELIANCE")
    scanner.mex = SimpleNamespace(universe={"ABC", "RELIANCE"})
    scanner.momentum_orb = SimpleNamespace(universe={"ABC", "RELIANCE"})
    scanner.live_engine = Mock()
    scanner.live_engine.detect.return_value = None
    scanner._live_symbol_locks = {"ABC": threading.RLock(), "RELIANCE": threading.RLock()}
    scanner._live_state_lock = threading.Lock()
    scanner._live_inflight = set()
    scanner._closed = False
    partial = Candle(datetime(2026, 11, 5, 10, 0, tzinfo=IST), 100, 102, 99, 101, 1000, 0)
    cached = {"session": [], "allc": [], "fut": []}
    now = datetime(2026, 11, 5, 10, 0, tzinfo=IST)

    with patch.object(scanner_module.cfg, "STRATEGY_MODE", "SIMPLE"):
        scanner._detect_live_candidate(symbol, partial, now, cached)
        scanner._detect_live_candidate(normal_symbol, partial, now, cached)

    assert scanner.live_engine.detect.call_count == 2
    outside_kwargs = scanner.live_engine.detect.call_args_list[0].kwargs
    normal_kwargs = scanner.live_engine.detect.call_args_list[1].kwargs
    assert outside_kwargs["allow_sequential"] is False
    assert outside_kwargs["allow_simple"] is False
    assert outside_kwargs["allow_mex"] is True
    assert outside_kwargs["allow_orb"] is True
    assert normal_kwargs["allow_simple"] is True


def test_signal_alert_requires_all_chat_acknowledgements():
    from models import Signal

    scanner = scanner_module.Scanner.__new__(scanner_module.Scanner)
    scanner.tg = SimpleNamespace(chat_ids=["chat-one", "chat-two"])
    scanner.db = Mock()
    scanner.db.telegram_delivery_status.return_value = {
        "total": 2, "delivered": 2, "pending": 0, "all_delivered": True
    }
    scanner._note_symbol = Mock()
    scanner._deliver_pending_outbox = Mock()
    scanner._mark_alerted = Mock()
    scanner._watch = Mock()
    now = datetime(2026, 11, 5, 10, 5, tzinfo=IST)
    signal = Signal(id="ABC-20261105T100000-C", ts="2026-11-05T10:00:00+05:30",
                    underlying="ABC", side="CALL", confidence=8.0)

    delivered = scanner._queue_signal_alert(signal, "dedupe-key", "alert body", now)

    assert delivered is True
    assert scanner.db.enqueue_telegram.call_count == 2
    scanner._deliver_pending_outbox.assert_called_once()
    scanner._mark_alerted.assert_called_once_with("dedupe-key")
    scanner._watch.assert_called_once()


def test_signal_alert_reports_outbox_enqueue_failure():
    from models import Signal

    scanner = scanner_module.Scanner.__new__(scanner_module.Scanner)
    scanner.tg = SimpleNamespace(chat_ids=["chat-one"])
    scanner.db = Mock()
    scanner.db.enqueue_telegram.side_effect = RuntimeError("database unavailable")
    scanner._note_symbol = Mock()
    scanner._deliver_pending_outbox = Mock()
    now = datetime(2026, 11, 5, 10, 5, tzinfo=IST)
    signal = Signal(id="ABC-20261105T100000-C", ts="2026-11-05T10:00:00+05:30",
                    underlying="ABC", side="CALL", confidence=8.0)

    delivered = scanner._queue_signal_alert(signal, "dedupe-key", "alert body", now)

    assert delivered is False
    scanner._note_symbol.assert_called_once()
    scanner._deliver_pending_outbox.assert_not_called()


def test_normal_15m_context_requires_matching_bullish_or_bearish_direction():
    matches = scanner_module._normal_15m_context_matches

    assert matches("CALL", 1) is True
    assert matches("CALL", -1) is False
    assert matches("CALL", 0) is True

    assert matches("PUT", -1) is True
    assert matches("PUT", 1) is False
    assert matches("PUT", 0) is True

    assert matches("UNKNOWN", 1) is False
    # V3 requires a directional completed 15m context; neutral/insufficient
    # history is not treated as alignment.
    assert matches("CALL", 0, allow_neutral=False) is False
    assert matches("PUT", 0, allow_neutral=False) is False



def test_normal_structural_invalidation_distance_is_available_for_both_directions():
    from strategies.price import invalidation_distance

    now = datetime(2026, 11, 5, 12, 0, tzinfo=IST)
    candles = []
    for i in range(40):
        close = 100.0 + i * 0.5
        candles.append(Candle(now - timedelta(minutes=5 * (39 - i)),
                              close - 0.2, close + 1.0, close - 1.0,
                              close, 1000, 0))

    call_distance = invalidation_distance("CALL", candles, candles, 120.0, "TREND")
    put_distance = invalidation_distance("PUT", candles, candles, 119.0, "TREND")

    assert call_distance > 0
    assert put_distance > 0


def test_simple_intrabar_rule_accepts_live_alignment_without_directional_candle_gate():
    from strategies import simple_signal

    now = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    completed = [
        Candle(now - timedelta(minutes=5 * (40 - i)), 100, 101, 99, 100, 1000, 0)
        for i in range(40)
    ]
    session = completed[-4:]
    # Red partial candle is intentional: SIMPLE must not require candle colour/body.
    partial = Candle(now, 103, 104, 100, 101, 300, 0)

    with patch.object(simple_signal, "evaluate_simple",
                      return_value={"side": 0, "metrics": {}, "gate_failures": []}), \
         patch.object(simple_signal, "ema", side_effect=lambda values, period: [100.5] if period == 9 else [100.0]), \
         patch.object(simple_signal, "rsi", return_value=[60.0]), \
         patch.object(simple_signal, "adx", return_value=[(30.0, 20.0, 10.0)]), \
         patch.object(simple_signal, "atr", return_value=[2.0]), \
         patch.object(simple_signal, "vwap", return_value=[100.5]):
        result = simple_signal.evaluate_simple_intrabar(
            session, completed, partial, min_bars=35, min_session_bars=3
        )

    assert result["side"] == 1
    assert result["metrics"]["mode"] == "INTRABAR"
    assert partial.c < partial.o


def test_nearest_structure_obstacle_detects_repeated_resistance_and_support():
    from strategies.price import nearest_structure_obstacle

    now = datetime(2026, 11, 5, 12, 0, tzinfo=IST)
    candles = []
    for i in range(40):
        high = 103.0
        low = 97.0
        if i in (25, 32):
            high = 110.0
        if i in (18, 28):
            low = 90.0
        close = 100.0
        candles.append(Candle(now - timedelta(minutes=5 * (39 - i)),
                              close, high, low, close, 1000, 0))

    assert nearest_structure_obstacle("CALL", candles, 105.0) == 110.0
    assert nearest_structure_obstacle("PUT", candles, 95.0) == 90.0


def test_simple_mode_skips_normal_detector_outside_fixed_universe():
    now = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    candle = Candle(now - timedelta(minutes=5), 100, 102, 99, 101, 1000, 0)
    symbol = Underlying("ABC", "STOCK", "NSE_EQ|ABC")
    scanner = _stage1_scanner(symbol, None)

    with (
        patch.object(scanner_module.cfg, "STRATEGY_MODE", "SIMPLE"),
        patch.object(scanner_module.events, "symbol_event", return_value=False),
        patch.object(scanner_module, "_stage1_fetch_results",
                     return_value=iter([(symbol, ([candle], None), None)])),
    ):
        candidates, hits = scanner.stage1(now)

    scanner._simple_candidate.assert_not_called()
    scanner.mex.scan.assert_called_once()
    scanner.momentum_orb.scan.assert_called_once()
    assert candidates == []
    assert hits == 0


def test_simple_structural_stop_includes_live_trigger_bar_extreme():
    from strategies import price

    now = datetime(2026, 11, 5, 12, 0, tzinfo=IST)
    candles = [
        Candle(now - timedelta(minutes=5 * (39 - i)),
               100.0, 102.0, 98.0, 100.0, 1000, 0)
        for i in range(40)
    ]
    live_call = Candle(now, 120.0, 120.5, 119.0, 120.2, 100, 0)
    live_put = Candle(now, 80.0, 81.0, 79.5, 80.2, 100, 0)

    with (
        patch.object(price, "atr", return_value=[1.0] * 40),
        patch.object(price, "ema", return_value=[110.0] * 40),
        patch.object(price, "vwap", return_value=[105.0] * 40),
    ):
        call_distance = price.invalidation_distance(
            "CALL", candles, candles, 120.0, "SIMPLE_TREND", trigger_bar=live_call)
        put_distance = price.invalidation_distance(
            "PUT", candles, candles, 80.0, "SIMPLE_TREND", trigger_bar=live_put)

    # STOP_BUFFER_ATR (0.25) is added beyond the selected live wick.
    assert call_distance == 1.25
    assert put_distance == 1.25
