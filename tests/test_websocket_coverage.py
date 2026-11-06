import asyncio
import json
import threading
import time

from data.feed_decoder import Tick
from data.upstox_ws import UpstoxWS
from bot import Bot


class _Socket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def test_status_reports_expected_sent_and_live_tick_coverage():
    ws = UpstoxWS(token_fn=lambda: "test-token")
    ws.keys.update({"NSE_EQ|A", "NSE_EQ|B"})
    ws.connected = True
    ws._sent_keys.update(ws.keys)
    ws._last_tick_by_key["NSE_EQ|A"] = 100.0
    ws.ticks["NSE_EQ|A"] = Tick(ltp=100.0)
    ws.last_msg = 100.0

    status = ws.status()

    assert status["expected_subscription_count"] == 2
    assert status["subscription_sent_count"] == 2
    assert status["expected_subscription_keys"] == ["NSE_EQ|A", "NSE_EQ|B"]
    assert status["subscription_sent_keys"] == ["NSE_EQ|A", "NSE_EQ|B"]
    assert status["subscription_state"] == "connected_sent"
    assert status["instruments_seen"] == 1
    assert status["tick_coverage_pct"] == 50.0
    assert status["missing_tick_keys"] == ["NSE_EQ|B"]


def test_subscription_send_batches_and_tracks_all_keys():
    ws = UpstoxWS(token_fn=lambda: "test-token")
    keys = [f"NSE_EQ|{i}" for i in range(205)]
    ws.keys.update(keys)
    ws.connected = True
    ws._ws = _Socket()

    asyncio.run(ws._send("sub", keys))

    assert len(ws._ws.sent) == 3
    assert all(json.loads(frame)["method"] == "sub" for frame in ws._ws.sent)
    assert ws.status()["subscription_sent_count"] == 205
    assert ws.status()["subscription_state"] == "connected_sent"


def test_unsubscribe_removes_keys_from_current_connection_sent_set():
    ws = UpstoxWS(token_fn=lambda: "test-token")
    ws.keys.update({"NSE_EQ|A", "NSE_EQ|B"})
    ws._sent_keys.update(ws.keys)
    ws.connected = True
    ws._ws = _Socket()

    asyncio.run(ws._send("unsub", ["NSE_EQ|A"]))

    assert ws.status()["subscription_sent_count"] == 1


def test_bootstrap_notifications_never_block_the_bootstrap_caller():
    entered = threading.Event()
    release = threading.Event()

    class _Telegram:
        last_error = None

        def send(self, _message):
            entered.set()
            release.wait(2.0)
            return True

    bot = object.__new__(Bot)
    bot._bootstrap_notice_lock = threading.Lock()
    bot._bootstrap_notice_thread = None
    bot._bootstrap_notice_status = {"state": "not_started"}
    bot.tg = _Telegram()

    started = time.monotonic()
    bot._send_bootstrap_notices(["test notice"])
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert entered.wait(1.0)
    release.set()
    bot._bootstrap_notice_thread.join(timeout=1.0)
    assert bot._bootstrap_notice_status["state"] == "sent"

def test_holiday_probe_never_blocks_the_scanner_tick_thread():
    from datetime import date

    entered = threading.Event()
    release = threading.Event()
    today = date(2026, 11, 4)

    bot = object.__new__(Bot)
    bot.day_checked = None
    bot._holiday_check_lock = threading.Lock()
    bot._holiday_check_thread = None
    bot._holiday_check_status = {"state": "idle"}

    def slow_probe(_today):
        entered.set()
        release.wait(2.0)

    bot._holiday_check = slow_probe
    started = time.monotonic()
    bot._schedule_holiday_check(today)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert entered.wait(1.0)
    release.set()
    bot._holiday_check_thread.join(timeout=1.0)
    assert bot._holiday_check_status["state"] == "unconfirmed"

def test_pending_telegram_retries_do_not_block_scan_cycle():
    from unittest.mock import patch
    from engine.scanner import Scanner

    entered = threading.Event()
    release = threading.Event()

    class _OutboxDB:
        def pending_telegram(self, _limit):
            entered.set()
            release.wait(2.0)
            return []

    scanner = object.__new__(Scanner)
    scanner._closed = False
    scanner._outbox_lock = threading.Lock()
    scanner._outbox_thread = None
    scanner._outbox_db = None
    scanner._outbox_status = {"state": "idle"}
    scanner._deliver_pending_outbox = lambda db=None: db.pending_telegram(100)

    started = time.monotonic()
    scanner._maintenance_db_lock = threading.Lock()
    patcher = patch("db.DB", return_value=_OutboxDB())
    patcher.start()
    try:
        scanner._schedule_pending_outbox_delivery()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert entered.wait(1.0)
        release.set()
        scanner._outbox_thread.join(timeout=1.0)
        assert scanner._outbox_status["state"] == "completed"
    finally:
        release.set()
        patcher.stop()

def test_archive_uses_async_writer_when_available():
    from engine.scanner import Scanner
    from models import Candle
    from datetime import datetime
    from utils.market_hours import IST

    class _Writer:
        def __init__(self):
            self.submitted = []

        def submit(self, symbol, candles):
            self.submitted.append((symbol, list(candles)))
            return True

    scanner = object.__new__(Scanner)
    scanner.history_writer = _Writer()
    scanner._archived = {}
    candle = Candle(ts=datetime(2026, 11, 4, 10, 0, tzinfo=IST), o=100, h=102, l=99, c=101, v=10, oi=0)

    scanner._archive("NIFTY", [candle])

    assert len(scanner.history_writer.submitted) == 1
    assert scanner._archived["NIFTY"] == candle.ts.isoformat()


def test_candidate_watch_updates_memory_without_synchronous_database_write():
    from engine.scanner import Scanner
    from datetime import datetime
    from utils.market_hours import IST

    class _DB:
        backend = "postgres"

        def kv_set(self, *_args):
            raise AssertionError("production candidate watch must not write to primary DB synchronously")

    scanner = object.__new__(Scanner)
    scanner.db = _DB()
    scanner.candidate_watchlist = {}
    scanner._candidate_lock = threading.RLock()
    scheduled = []
    scanner._schedule_candidate_watchlist_persistence = lambda: scheduled.append(True)

    scanner._watch("NIFTY", "CALL", datetime(2026, 11, 4, 10, 0, tzinfo=IST), 0.8,
                   "WATCHING", reasons=["test"], now=datetime(2026, 11, 4, 10, 5, tzinfo=IST))

    assert scanner.candidate_watchlist["NIFTY:CALL"]["status"] == "WATCHING"
    assert scheduled == [True]


def test_candidate_watchlist_persistence_is_nonblocking_and_coalesced():
    from engine.scanner import Scanner
    from unittest.mock import patch

    entered = threading.Event()
    release = threading.Event()

    class _MaintenanceDB:
        def kv_set(self, _key, _value):
            entered.set()
            release.wait(2.0)

    scanner = object.__new__(Scanner)
    scanner._closed = False
    scanner._maintenance_db_lock = threading.Lock()
    scanner._candidate_persist_lock = threading.Lock()
    scanner._candidate_lock = threading.RLock()
    scanner._candidate_persist_thread = None
    scanner._candidate_persist_dirty = False
    scanner._candidate_persist_status = {"state": "idle"}
    scanner._outbox_db = None
    scanner.candidate_watchlist = {"NIFTY:CALL": {"status": "WATCHING"}}

    started = time.monotonic()
    patcher = patch("db.DB", return_value=_MaintenanceDB())
    patcher.start()
    try:
        scanner._schedule_candidate_watchlist_persistence()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert entered.wait(1.0)
        release.set()
        scanner._candidate_persist_thread.join(timeout=1.0)
        assert scanner._candidate_persist_status["state"] == "completed"
    finally:
        release.set()
        patcher.stop()
