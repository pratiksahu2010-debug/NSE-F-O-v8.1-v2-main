from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.entry_validation import validate_entry_candidate
from engine.live_intrabar import LiveIntrabarEngine
from models import Underlying

IST = ZoneInfo("Asia/Kolkata")


def test_live_bar_uses_cumulative_volume_deltas_and_keeps_partial_separate():
    engine = LiveIntrabarEngine(scan_minutes=5, stale_seconds=8)
    now = datetime(2026, 11, 3, 10, 1, tzinfo=IST)
    first = engine.update("ABC", 100.0, 1000.0, now)
    second = engine.update("ABC", 101.0, 1012.0, now + timedelta(seconds=1))
    third = engine.update("ABC", 100.5, 1019.0, now + timedelta(seconds=2))

    assert first.ts == datetime(2026, 11, 3, 10, 0, tzinfo=IST)
    assert third.o == 100.0
    assert third.h == 101.0
    assert third.l == 100.0
    assert third.c == 100.5
    assert third.v == 19.0
    assert second.v == 12.0


def test_live_snapshot_rejects_stale_tick():
    engine = LiveIntrabarEngine(scan_minutes=5, stale_seconds=5)
    now = datetime(2026, 11, 3, 10, 1, tzinfo=IST)
    engine.update("ABC", 100.0, 100.0, now)
    assert engine.snapshot("ABC", now + timedelta(seconds=4)) is not None
    assert engine.snapshot("ABC", now + timedelta(seconds=6)) is None


def test_live_intrabar_entry_validation_uses_confirmation_age_not_closed_bar_time():
    now = datetime.now(IST)
    partial_open = now.replace(second=0, microsecond=0)
    setup = partial_open - timedelta(minutes=5)
    candidate = {
        "symbol": "ABC",
        "strategy": "V7_SEQUENTIAL",
        "side": "CALL",
        "setup_time": setup.isoformat(),
        "trigger_time": partial_open.isoformat(),
        "confirmation_time": now.isoformat(),
        "confirmation_price": 100.0,
        "mode": "INTRABAR",
    }
    partial = type("Partial", (), {"ts": partial_open})()
    valid, reason, _ = validate_entry_candidate(
        candidate, partial, 100.05, 1.0, 0.4, 0.6
    )
    assert valid
    assert reason == "ENTRY_VALID"


def test_live_intrabar_engine_requires_closed_history_before_detection():
    engine = LiveIntrabarEngine()
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    now = datetime(2026, 11, 3, 10, 1, tzinfo=IST)
    partial = engine.update("ABC", 100.0, 1000.0, now)
    assert engine.detect(und, [], [], partial, now, object()) is None



def test_live_option_chain_refresh_is_nonblocking_and_reuses_fresh_result():
    from concurrent.futures import Future
    import threading
    from engine.scanner import Scanner

    class Pool:
        def __init__(self):
            self.future = Future()
            self.calls = []

        def submit(self, fn, *args):
            self.calls.append((fn, args))
            return self.future

    class Provider:
        def call(self, *args):
            return "fresh-chain"

    scanner = Scanner.__new__(Scanner)
    scanner._live_chain_lock = threading.Lock()
    scanner._live_chain_cache = {}
    scanner._live_chain_inflight = {}
    scanner._live_chain_retry_after = {}
    scanner._live_chain_pool = Pool()
    scanner.fo = Provider()
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    expiry = datetime(2026, 11, 26, tzinfo=IST).date()

    # First call schedules REST work and immediately returns; it never waits.
    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) is None
    assert len(scanner._live_chain_pool.calls) == 1

    scanner._live_chain_pool.future.set_result("fresh-chain")
    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) == "fresh-chain"
    # The just-completed result is cached for the short TTL.
    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) == "fresh-chain"
    assert len(scanner._live_chain_pool.calls) == 1


def test_live_option_chain_failed_refresh_is_throttled():
    from concurrent.futures import Future
    import threading
    from engine.scanner import Scanner

    class Pool:
        def __init__(self):
            self.futures = []

        def submit(self, fn, *args):
            future = Future()
            self.futures.append(future)
            return future

    class Provider:
        def call(self, *args):
            raise RuntimeError("network failure")

    scanner = Scanner.__new__(Scanner)
    scanner._live_chain_lock = threading.Lock()
    scanner._live_chain_cache = {}
    scanner._live_chain_inflight = {}
    scanner._live_chain_retry_after = {}
    scanner._live_chain_pool = Pool()
    scanner.fo = Provider()
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    expiry = datetime(2026, 11, 26, tzinfo=IST).date()

    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) is None
    scanner._live_chain_pool.futures[0].set_exception(RuntimeError("network failure"))
    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) is None
    assert len(scanner._live_chain_pool.futures) == 1



def test_live_candidate_reservation_is_released_when_symbol_lock_is_busy():
    import threading
    from engine.scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner._live_state_lock = threading.Lock()
    key = "ABC:V7_SEQUENTIAL:CALL:setup"
    scanner._live_inflight = {key}
    symbol_lock = threading.Lock()
    symbol_lock.acquire()
    scanner._live_symbol_locks = {"ABC": symbol_lock}
    candidate = {"setup_id": key}
    try:
        scanner._evaluate_live_candidate(Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC"),
                                         candidate, {})
        assert key not in scanner._live_inflight
    finally:
        symbol_lock.release()


def test_live_option_chain_empty_result_is_throttled():
    from concurrent.futures import Future
    import threading
    from engine.scanner import Scanner

    class Pool:
        def __init__(self):
            self.future = Future()
            self.calls = 0

        def submit(self, fn, *args):
            self.calls += 1
            return self.future

    class Provider:
        def call(self, *args):
            return None

    scanner = Scanner.__new__(Scanner)
    scanner._live_chain_lock = threading.Lock()
    scanner._live_chain_cache = {}
    scanner._live_chain_inflight = {}
    scanner._live_chain_retry_after = {}
    scanner._live_chain_pool = Pool()
    scanner.fo = Provider()
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    expiry = datetime(2026, 11, 26, tzinfo=IST).date()

    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) is None
    scanner._live_chain_pool.future.set_result(None)
    assert scanner._live_option_chain(und, expiry, datetime.now(IST)) is None
    assert scanner._live_chain_pool.calls == 1


def test_live_detector_reads_partial_candle_volume_from_v_field():
    """Regression: Candle exposes volume as v, not volume."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from engine import live_intrabar as live_module
    from models import Candle

    engine = LiveIntrabarEngine()
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    now = datetime(2026, 11, 3, 13, 25, tzinfo=IST)
    start = datetime(2026, 11, 3, 9, 15, tzinfo=IST)
    completed = [
        Candle(start + timedelta(minutes=5 * i), 100.0, 100.5, 99.5, 100.0, 1000.0)
        for i in range(50)
    ]
    partial = Candle(now.replace(minute=now.minute - now.minute % 5), 100.0,
                     100.5, 99.5, 100.0, 25.0)
    cfg = SimpleNamespace(
        V7_MIN_BARS=50, SETUP_MIN_ADX=20.0, DI_MIN_GAP=2.0,
        MOMENTUM_ORB_ENABLED=False, MEX_ENABLED=False,
    )

    with (
        patch.object(live_module, "ema", return_value=[100.0] * 50),
        patch.object(live_module, "atr", return_value=[1.0] * 50),
        patch.object(live_module, "vwap", return_value=[100.0] * 50),
        patch.object(live_module, "adx", return_value=[(25.0, 20.0, 10.0)] * 50),
        patch.object(live_module, "rsi", return_value=[55.0] * 50),
    ):
        result = engine.detect(
            und, completed, completed, partial, now, cfg,
            allow_sequential=False, allow_mex=False, allow_orb=False,
        )

    assert result is None


def _live_detector_fixture():
    from datetime import datetime, timedelta
    from models import Candle
    now = datetime(2026, 11, 3, 10, 1, tzinfo=IST)
    session_start = datetime(2026, 11, 3, 9, 15, tzinfo=IST)
    prior_start = datetime(2026, 11, 2, 12, 0, tzinfo=IST)
    prior = [
        Candle(prior_start + timedelta(minutes=5 * i), 100.0, 100.25, 99.75, 100.0, 1000.0)
        for i in range(41)
    ]
    session = []
    for i in range(9):
        ts = session_start + timedelta(minutes=5 * i)
        if i < 3:
            session.append(Candle(ts, 100.0, 102.0, 98.0, 101.0, 1000.0))
        else:
            session.append(Candle(ts, 101.0, 101.25, 100.75, 101.0, 1000.0))
    return now, session, prior + session


def test_live_orb_triggers_on_partial_breakout_with_50_percent_body_and_65_percent_close():
    from types import SimpleNamespace
    from unittest.mock import patch
    from engine import live_intrabar as live_module
    from models import Candle

    now, session, completed = _live_detector_fixture()
    partial = Candle(now.replace(minute=0, second=0, microsecond=0),
                     102.0, 102.8, 101.9, 102.7, 50.0)
    cfg = SimpleNamespace(
        V7_MIN_BARS=50, MOMENTUM_ORB_ENABLED=True, MEX_ENABLED=False,
        MEX_ORB_LAST_TIME=(14, 45), ORB_BARS=3,
        ORB_MIN_RANGE_ATR=0.6, ORB_MAX_RANGE_ATR=3.5,
        MEX_ORB_BODY_MIN=0.50, MEX_ORB_CLOSE_POS=0.65,
        MEX_ORB_CLEARANCE_ATR=0.05, MEX_ORB_MAX_EXTENSION_ATR=1.5,
        LIVE_MAX_RUNAWAY_ATR=0.8,
    )
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    with (
        patch.object(live_module, "ema", side_effect=lambda values, period: [102.0] * len(values) if period == 9 else [100.0] * len(values)),
        patch.object(live_module, "atr", return_value=[1.2] * len(completed)),
        patch.object(live_module, "vwap", return_value=[100.0] * len(session)),
        patch.object(live_module, "adx", return_value=[(25.0, 20.0, 10.0)] * len(completed)),
        patch.object(live_module, "rsi", return_value=[55.0] * len(completed)),
    ):
        engine = LiveIntrabarEngine()
        result = engine.detect(
            und, session, completed, partial, now, cfg,
            allow_sequential=False, allow_mex=False, allow_orb=True,
        )
    assert result is not None
    assert result["strategy"] == "MOMENTUM_ORB"
    assert result["side"] == "CALL"
    assert result["metrics"]["body_fraction"] >= 0.50
    assert result["metrics"]["close_position"] >= 0.65


def test_live_mex_uses_configured_impulse_and_range_expansion_without_macd_veto():
    from types import SimpleNamespace
    from unittest.mock import patch
    from engine import live_intrabar as live_module
    from models import Candle

    now, session, completed = _live_detector_fixture()
    # Last eight completed candles are a tight base; the live partial candle
    # expands 2x, breaks its high and produces a 0.8 ATR directional impulse.
    partial = Candle(now.replace(minute=0, second=0, microsecond=0),
                     101.7, 102.6, 101.6, 102.5, 20.0)
    cfg = SimpleNamespace(
        V7_MIN_BARS=50, MOMENTUM_ORB_ENABLED=False, MEX_ENABLED=True,
        MEX_LOOKBACK=8, MEX_COMPRESSION_MAX=0.90,
        MEX_MIN_RANGE_RATIO=1.5, MEX_MIN_IMPULSE_ATR=0.45,
        MEX_CLOSE_POS=0.75, MEX_MAX_EXTENSION_ATR=1.8,
        LIVE_MAX_RUNAWAY_ATR=0.8,
    )
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    with (
        patch.object(live_module, "ema", side_effect=lambda values, period: [101.5] * len(values) if period == 9 else [100.0] * len(values)),
        patch.object(live_module, "atr", return_value=[1.0] * len(completed)),
        patch.object(live_module, "vwap", return_value=[100.0] * len(session)),
        patch.object(live_module, "adx", return_value=[(25.0, 20.0, 10.0)] * len(completed)),
        patch.object(live_module, "rsi", return_value=[55.0] * len(completed)),
        patch.object(live_module, "macd", return_value=([], [], [-1.0, -2.0])),
    ):
        engine = LiveIntrabarEngine()
        result = engine.detect(
            und, session, completed, partial, now, cfg,
            allow_sequential=False, allow_mex=True, allow_orb=False,
        )
    assert result is not None
    assert result["strategy"] == "MOMENTUM_EXPLOSION"
    assert result["side"] == "CALL"
    assert result["metrics"]["range_expansion"] >= 1.5
    assert result["metrics"]["impulse_atr"] >= 0.45
    assert result["metrics"]["macd_supporting"] is False



def test_simple_live_detector_uses_simple_history_floor_not_v7_floor():
    from types import SimpleNamespace
    from unittest.mock import patch
    from engine import live_intrabar as live_module
    from models import Candle

    now = datetime(2026, 11, 3, 10, 1, tzinfo=IST)
    session = [
        Candle(datetime(2026, 11, 3, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i),
               100.0, 101.0, 99.0, 100.5, 1000.0)
        for i in range(3)
    ]
    completed = [
        Candle(datetime(2026, 11, 2, 12, 0, tzinfo=IST) + timedelta(minutes=5 * i),
               100.0, 101.0, 99.0, 100.5, 1000.0)
        for i in range(35)
    ]
    partial = Candle(datetime(2026, 11, 3, 10, 0, tzinfo=IST),
                     100.5, 101.0, 100.4, 100.9, 10.0)
    cfg = SimpleNamespace(
        V7_MIN_BARS=50, SIMPLE_MIN_BARS=35,
        MOMENTUM_ORB_ENABLED=False, MEX_ENABLED=False,
        LIVE_MAX_RUNAWAY_ATR=0.8, SIMPLE_ADX_MIN=25.0,
        SIMPLE_RSI_CALL_MIN=52.0, SIMPLE_RSI_CALL_MAX=68.0,
        SIMPLE_RSI_PUT_MIN=32.0, SIMPLE_RSI_PUT_MAX=48.0,
        SIMPLE_FRESH_BARS=3, SIMPLE_MIN_SESSION_BARS=3,
        SIMPLE_MAX_EXTENSION_ATR=1.5, HTF_MINUTES=15,
    )
    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    expected = {"side": 1, "reason": "early simple trigger", "metrics": {"trigger_edge": 100.9}}

    with (
        patch.object(live_module, "ema", side_effect=lambda values, period: [101.0] * len(values)),
        patch.object(live_module, "atr", return_value=[1.0] * 35),
        patch.object(live_module, "vwap", return_value=[100.0] * len(session)),
        patch.object(live_module, "adx", return_value=[(25.0, 20.0, 10.0)] * 35),
        patch.object(live_module, "rsi", return_value=[55.0] * 35),
        patch.object(live_module, "htf_bias", return_value=0),
        patch.object(live_module, "evaluate_simple_intrabar", return_value=expected),
    ):
        engine = LiveIntrabarEngine()
        result = engine.detect(
            und, session, completed, partial, now, cfg,
            allow_sequential=False, allow_mex=False, allow_orb=False,
            allow_simple=True,
        )

    assert result is not None
    assert result["strategy"] == "SIMPLE_TREND"
    assert result["side"] == "CALL"
    live_stats = engine.normal_snapshot()
    assert live_stats["evaluated"] == 1
    assert live_stats["detected"] == 1
    assert live_stats["setups"]["SIMPLE_TREND"] == 1


def test_simple_live_candidate_dispatch_accepts_simple_trend_strategy():
    import threading
    from datetime import date
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from engine.scanner import Scanner
    from models import Candle

    scanner = Scanner.__new__(Scanner)
    scanner._live_state_lock = threading.Lock()
    scanner._live_inflight = set()
    scanner._live_symbol_locks = {"ABC": threading.Lock()}
    scanner._closed = False
    scanner.tracker = SimpleNamespace(signals=lambda: [])
    scanner.master = object()
    scanner._live_option_chain = Mock(return_value=object())
    scanner._evaluate_locked = Mock(return_value=None)
    scanner.live_engine = SimpleNamespace(mark_sent=Mock())

    und = Underlying(name="ABC", kind="STOCK", spot_key="NSE_EQ|ABC")
    now = datetime(2026, 11, 5, 10, 1, tzinfo=IST)
    partial = Candle(now.replace(minute=0, second=0, microsecond=0),
                     100.0, 101.0, 99.5, 100.8, 10.0)
    candidate = {
        "setup_id": "ABC:SIMPLE_TREND:CALL:2026-11-05T10:00:00+05:30",
        "strategy": "SIMPLE_TREND", "side": "CALL",
        "confirmation_time": now.isoformat(), "bar": partial,
        "reason": "early SIMPLE trigger",
    }
    with patch("engine.scanner.pick_expiry", return_value=date(2026, 11, 26)):
        scanner._evaluate_live_candidate(und, candidate, {"session": [], "allc": [], "fut": []})

    assert scanner._evaluate_locked.called
    evaluated = scanner._evaluate_locked.call_args.args[0]
    assert evaluated["votes"][0].name == "SIMPLE_TREND"
    assert evaluated["entry_candidate"]["mode"] == "INTRABAR"
