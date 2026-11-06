"""Tests for token automation, feed decoding, streaming master parse, DB upserts, HTTP/cron surface and tick()."""
import gzip
import io
import json
import os
import sqlite3
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import tokens as T  # noqa: E402
from config import cfg  # noqa: E402
from data import feed_decoder  # noqa: E402
from data.failover import FailoverManager  # noqa: E402
from data.instruments import Master, iter_json_array  # noqa: E402
from db import DB  # noqa: E402
from models import Candle, Signal  # noqa: E402
from data.history_persistence import AsyncCandleWriter  # noqa: E402
from utils.market_hours import IST  # noqa: E402
from engine.scanner import _stage1_fetch_results  # noqa: E402


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return DB(connection=conn)


def make_jwt(exp):
    import base64
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS256'})}.{b({'exp': exp})}.sig"


class Cfg:
    """Temporarily override cfg attributes."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(cfg, k)
            setattr(cfg, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(cfg, k, v)


class TestTokens(unittest.TestCase):
    def setUp(self):
        self.alerts = []
        self.tm = T.TokenManager(alert=self.alerts.append)

    def _wait_restore(self, tm):
        deadline = time.time() + 2.0
        while tm._persist_loading and time.time() < deadline:
            time.sleep(0.005)
        self.assertFalse(tm._persist_loading, "token restore worker did not finish")

    def test_persisted_token_restore_is_background_and_does_not_overwrite_webhook(self):
        entered, release = threading.Event(), threading.Event()

        class SlowKV:
            def kv_get(self, key):
                entered.set()
                release.wait(1.0)
                return json.dumps({"token": "PERSISTED", "exp": time.time() + 3600})

            def kv_set(self, key, value):
                return None

        tm = T.TokenManager(kv_factory=SlowKV)
        started = time.monotonic()
        tm._restore_persisted()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(entered.wait(0.5))
        tm.set_upstox("WEBHOOK", time.time() + 7200)
        release.set()
        self._wait_restore(tm)
        self.assertEqual(tm._up_runtime[0], "WEBHOOK")
        self.assertTrue(tm._persist_loaded)

    def test_persisted_token_restore_retries_transient_kv_failure(self):
        class FlakyKV:
            def __init__(self):
                self.failed = False

            def kv_get(self, key):
                if key == "runtime_upstox_token" and not self.failed:
                    self.failed = True
                    raise RuntimeError("temporary database outage")
                return None

            def kv_set(self, key, value):
                return None

        kv = FlakyKV()
        tm = T.TokenManager(kv_factory=lambda: kv)
        tm._restore_persisted()
        self._wait_restore(tm)
        self.assertFalse(tm._persist_loaded)
        self.assertIn("temporary database outage", tm._persist_restore_error)
        tm._persist_retry_after = 0
        tm._restore_persisted()
        self._wait_restore(tm)
        self.assertTrue(tm._persist_loaded)
        self.assertIsNone(tm._persist_restore_error)

    def test_totp_rfc6238_vector(self):
        self.assertEqual(T.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59, digits=8), "94287082")
        self.assertEqual(len(T.totp("JBSWY3DPEHPK3PXP")), 6)

    def test_jwt_exp(self):
        self.assertEqual(T.jwt_exp(make_jwt(1900000000)), 1900000000.0)
        self.assertIsNone(T.jwt_exp("not-a-jwt"))

    def test_analytics_token_preferred_and_expiry_warning(self):
        exp = time.time() + 5 * 86400
        with Cfg(UPSTOX_ANALYTICS_TOKEN=make_jwt(exp), UPSTOX_ACCESS_TOKEN="legacy"):
            self.assertEqual(self.tm.upstox_source()[0], "analytics")
            r = self.tm.ensure_fresh()
            self.assertTrue(r["upstox"]["ok"])
            self.assertTrue(any("expires in" in a for a in self.alerts), self.alerts)

    def test_expired_analytics_falls_back_and_alerts(self):
        with Cfg(UPSTOX_ANALYTICS_TOKEN=make_jwt(time.time() - 10), UPSTOX_ACCESS_TOKEN=make_jwt(time.time() + 3600)):
            self.assertEqual(self.tm.upstox_source()[0], "env")
        with Cfg(UPSTOX_ANALYTICS_TOKEN=make_jwt(time.time() - 10), UPSTOX_ACCESS_TOKEN=""):
            self.tm.ensure_fresh()
            self.assertTrue(any("missing/expired" in a for a in self.alerts))

    def test_webhook_sets_runtime_token(self):
        with Cfg(UPSTOX_API_KEY="KEY", UPSTOX_ANALYTICS_TOKEN="", UPSTOX_ACCESS_TOKEN=""):
            self.assertFalse(self.tm.handle_upstox_webhook({"message_type": "access_token", "client_id": "OTHER", "access_token": "x"}))
            exp_ms = str(int((time.time() + 3600) * 1000))
            self.assertTrue(self.tm.handle_upstox_webhook({"message_type": "access_token", "client_id": "KEY",
                                                           "access_token": "NEWTOK", "expires_at": exp_ms}))
            self.assertEqual(self.tm.upstox_token(), "NEWTOK")

    def test_dhan_totp_generate(self):
        resp = mock.Mock(ok=True, content=b"1")
        resp.json.return_value = {"accessToken": "DHANTOK", "expiryTime": (datetime.now(IST) + timedelta(hours=24)).isoformat()}
        with Cfg(DHAN_CLIENT_ID="1000", DHAN_PIN="123456", DHAN_TOTP_SECRET="JBSWY3DPEHPK3PXP", DHAN_ACCESS_TOKEN=""), \
                mock.patch.object(T.requests, "post", return_value=resp) as post:
            self.assertTrue(self.tm.refresh_dhan())
            self.assertEqual(self.tm.dhan_token(), "DHANTOK")
            params = post.call_args.kwargs["params"]
            self.assertEqual(params["dhanClientId"], "1000")
            self.assertEqual(len(params["totp"]), 6)
            post.reset_mock()
            self.assertTrue(self.tm.refresh_dhan())      # still fresh -> no second call
            post.assert_not_called()

    def test_live_token_refresh_is_upstox_only(self):
        with Cfg(DHAN_CLIENT_ID="1000", DHAN_PIN="1", DHAN_TOTP_SECRET="JBSWY3DPEHPK3PXP"):
            self.tm._dhan = ("OLD", time.time() + 3 * 3600)
            with mock.patch.object(self.tm, "refresh_dhan") as refresh_dhan:
                self.tm.ensure_fresh(morning=True)
            refresh_dhan.assert_not_called()
            self.assertEqual(self.tm.dhan_token(), "OLD")
            self.assertEqual(self.tm.status()["provider_mode"], "upstox_only")

    def test_invalid_notifier_url_has_actionable_guidance(self):
        response = mock.Mock(ok=False, text='{"errors":[{"errorCode":"UDAPI1123","message":"Invalid notifier url"}]}')
        response.json.return_value = {"errors": [{"errorCode": "UDAPI1123", "message": "Invalid notifier url"}]}
        with Cfg(UPSTOX_API_KEY="KEY", UPSTOX_API_SECRET="SECRET"), \
                mock.patch.object(T.requests, "post", return_value=response):
            self.assertFalse(self.tm.request_upstox_token(force=True))
        self.assertTrue(any("notifier URL" in msg for msg in self.alerts), self.alerts)


def enc_varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def f_d(num, v): return enc_varint(num << 3 | 1) + struct.pack("<d", v)
def f_v(num, v): return enc_varint(num << 3) + enc_varint(v)
def f_m(num, b): return enc_varint(num << 3 | 2) + enc_varint(len(b)) + b


class TestFeed(unittest.TestCase):
    def test_decode_option_full_feed_index_and_market_info(self):
        ltpc = f_d(1, 123.5) + f_v(2, 1700000000000)
        quote = f_v(1, 50) + f_d(2, 123.0) + f_v(3, 75) + f_d(4, 124.0)
        mff = f_m(1, ltpc) + f_m(2, f_m(1, quote)) + f_m(3, f_d(1, 0.55)) + f_v(6, 9000) + f_d(7, 120000.0) + f_d(8, 14.2)
        opt = f_m(2, f_m(1, mff))                                  # Feed.fullFeed.marketFF
        idx = f_m(2, f_m(2, f_m(1, f_d(1, 25010.5))))               # Feed.fullFeed.indexFF
        first = f_m(3, f_m(1, ltpc) + f_m(2, quote) + f_d(5, 777.0))   # firstLevelWithGreeks
        entry = lambda k, v: f_m(2, f_m(1, k.encode()) + f_m(2, v))
        market_info = f_m(4, f_m(1, f_m(1, b"NSE_FO") + f_v(2, 2)))   # map<string, enum>: must be skipped, not crash
        raw = f_v(1, 1) + entry("NSE_FO|1", opt) + entry("NSE_INDEX|Nifty 50", idx) + entry("NSE_FO|2", first) + market_info
        out = feed_decoder.decode(raw)
        o = out["NSE_FO|1"]
        self.assertEqual((o.ltp, o.bid, o.ask, o.volume, o.oi, o.iv, o.delta), (123.5, 123.0, 124.0, 9000.0, 120000.0, 14.2, 0.55))
        self.assertEqual(out["NSE_INDEX|Nifty 50"].ltp, 25010.5)
        self.assertEqual((out["NSE_FO|2"].ltp, out["NSE_FO|2"].oi, out["NSE_FO|2"].ask), (123.5, 777.0, 124.0))

    def test_heartbeat_and_garbage(self):
        self.assertEqual(feed_decoder.decode(f_v(1, 2)), {})
        self.assertEqual(feed_decoder.decode(b""), {})


class TestMasterStreaming(unittest.TestCase):
    def test_iter_json_array_across_chunk_boundaries(self):
        rows = [{"segment": "NSE_FO", "i": i, "s": "x" * (i % 50), "nested": {"a": [1, 2, {"b": "]"}]}} for i in range(500)]
        txt = json.dumps(rows, indent=1)
        for chunk in (7, 64, 1000, 1 << 20):
            got = list(iter_json_array(io.StringIO(txt), chunk=chunk))
            self.assertEqual(got, rows, chunk)

    def test_strike_step_index(self):
        d = date(2026, 10, 27)
        ms = int(datetime(2026, 10, 27, 15, 30, tzinfo=IST).timestamp() * 1000)
        rows = [{"segment": "NSE_FO", "instrument_type": t, "underlying_symbol": "NIFTY", "underlying_key": "K", "underlying_type": "INDEX",
                 "expiry": ms, "strike_price": 25000 + 50 * k, "instrument_key": f"{k}{t}", "lot_size": 75}
                for k in range(-4, 5) for t in ("CE", "PE")]
        self.assertEqual(Master(rows).strike_step("NIFTY", d), 50)


class TestDB(unittest.TestCase):
    def test_missing_database_url_fails_fast_without_local_fallback(self):
        with Cfg(DATABASE_URL=""):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                DB()

    def test_url_normalization(self):
        self.assertEqual(DB._normalize_url("postgres://u:p@h:5432/d"), "postgresql://u:p@h:5432/d")
        self.assertEqual(DB._normalize_url("postgresql+psycopg2://u:p@h/d"), "postgresql://u:p@h/d")
        self.assertEqual(DB._normalize_url("  postgresql://u@h/d "), "postgresql://u@h/d")
        self.assertEqual(DB._normalize_url(None), "")

    def test_kv_upserts_and_signal_history(self):
        db = make_test_db()
        self.assertIsNone(db.kv_get("a"))
        db.kv_set("a", "1")
        db.kv_set("a", "2")
        self.assertEqual(db.kv_get("a"), "2")
        db.save_iv("2026-10-15", "NIFTY", 12.0)
        db.save_iv("2026-10-15", "NIFTY", 13.0)
        self.assertEqual(db.iv_hist("NIFTY"), [13.0])
        db.save_daily("2026-10-15", 3, 2, 1, 100.0)
        db.save_daily("2026-10-15", 4, 2, 2, 50.0)
        s = Signal(id="x", ts="2026-10-15T10:00:00", underlying="NIFTY", side="CALL")
        db.save_signal(s)
        s.status = "CLOSED"
        db.save_signal(s)
        self.assertEqual([x.status for x in db.signals_on("2026-10-15")], ["CLOSED"])
        self.assertEqual(db.last_signal_ts("NIFTY", "2026-10-15"), "2026-10-15T10:00:00")
        self.assertEqual(db.backend, "test-injected")

    def test_remote_row_mapping_and_outbox(self):
        db = make_test_db()
        db.enqueue_telegram("d1", "s1", "chat", "hello")
        pending = db.pending_telegram()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["message"], "hello")
        db.mark_telegram_delivered(pending[0]["id"])
        self.assertTrue(db.telegram_delivery_status("d1")["all_delivered"])

    # ---- PostgreSQL driver behaviour, exercised through a fake psycopg module backed by in-memory SQLite ----
    @staticmethod
    def _fake_psycopg(raw, connections=None):
        import types

        class OperationalError(Exception):
            pass
        OperationalError.__module__ = "psycopg.errors"

        class FakeConn:
            """Translates psycopg's %s / %% syntax back to SQLite so the real SQL path is exercised."""

            def __init__(self):
                self.raw = raw
                self.fail_next = False
                self.closed = False
                self.rollbacks = 0

            @staticmethod
            def _t(sql):
                return sql.replace("%s", "?").replace("%%", "%")

            def _maybe_fail(self):
                if self.fail_next:
                    self.fail_next = False
                    raise OperationalError("server closed the connection unexpectedly")

            def execute(self, sql, args=()):
                self._maybe_fail()
                return self.raw.execute(self._t(sql), args)

            def cursor(self):
                return self

            def executemany(self, sql, rows):
                self._maybe_fail()
                return self.raw.executemany(self._t(sql), rows)

            def commit(self):
                return self.raw.commit()

            def rollback(self):
                self.rollbacks += 1

            def close(self):
                self.closed = True

        made = []

        def connect(url, **kw):
            conn = FakeConn()
            made.append((url, kw, conn))
            return conn

        mod = types.ModuleType("psycopg")
        mod.connect = connect
        mod.OperationalError = OperationalError
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = object()
        mod.rows = rows
        return mod, rows, made

    def _pg_db(self, url="postgres://user:pw@host/db"):
        raw = sqlite3.connect(":memory:", check_same_thread=False)
        raw.row_factory = sqlite3.Row
        mod, rows, made = self._fake_psycopg(raw)
        with Cfg(DATABASE_URL=url):
            with mock.patch.dict(sys.modules, {"psycopg": mod, "psycopg.rows": rows}):
                db = DB()
        # reconnects import psycopg lazily, so keep the fake available to the caller
        self._mods = {"psycopg": mod, "psycopg.rows": rows}
        return db, made

    def test_postgres_driver_receives_normalized_url_and_creates_schema(self):
        db, made = self._pg_db()
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][0], "postgresql://user:pw@host/db")
        self.assertIn("statement_timeout", made[0][1]["options"])
        self.assertEqual(db.backend, "postgres")
        self.assertIsNone(db.kv_get("missing"))
        db.kv_set("k", "v")
        self.assertEqual(db.kv_get("k"), "v")

    def test_literal_percent_in_sql_survives_placeholder_translation(self):
        db, _ = self._pg_db()
        db.kv_set("alert:2026-10-01:X", "1")
        db.kv_set("alert:2026-10-30:Y", "1")
        db.kv_set("other", "1")
        db._x("DELETE FROM kv WHERE k LIKE 'alert:%' AND substr(k, 7, 10) < ?", ("2026-10-15",))
        self.assertIsNone(db.kv_get("alert:2026-10-01:X"))
        self.assertEqual(db.kv_get("alert:2026-10-30:Y"), "1")
        self.assertEqual(db.kv_get("other"), "1")

    def test_dropped_connection_query_reconnects_and_retries_exactly_once(self):
        db, made = self._pg_db()
        made[0][2].fail_next = True
        with mock.patch.dict(sys.modules, self._mods):
            db.kv_set("retry-query", "ok")
            self.assertEqual(db.kv_get("retry-query"), "ok")
        self.assertEqual(len(made), 2)
        self.assertTrue(made[0][2].closed)

    def test_dropped_connection_batch_insert_reconnects_and_retries_once(self):
        from types import SimpleNamespace
        db, made = self._pg_db()
        made[0][2].fail_next = True
        candle = SimpleNamespace(ts=datetime(2026, 11, 2, 10, 0), o=100.0, h=101.0,
                                 l=99.0, c=100.5, v=1000.0, oi=10.0)
        with mock.patch.dict(sys.modules, self._mods):
            db.save_candles("NIFTY", [candle])
        self.assertEqual(len(db.candles_between("NIFTY", "2026-11-02", "2026-11-02")), 1)
        self.assertEqual(len(made), 2)

    def test_sql_errors_roll_back_and_are_not_retried(self):
        db, made = self._pg_db()
        with self.assertRaises(Exception):
            db._q("SELECT * FROM table_that_does_not_exist")
        self.assertEqual(len(made), 1)
        self.assertGreaterEqual(made[0][2].rollbacks, 1)
        db.kv_set("still", "works")
        self.assertEqual(db.kv_get("still"), "works")

    def test_only_connection_errors_are_classified_as_retryable(self):
        class OperationalError(Exception):
            pass
        OperationalError.__module__ = "psycopg.errors"
        self.assertTrue(DB._is_connection_error(OperationalError("server closed the connection")))
        self.assertFalse(DB._is_connection_error(RuntimeError("relation does not exist")))
        self.assertFalse(DB._is_connection_error(sqlite3.OperationalError("no such table")))


class Prov:
    def __init__(self, name, configured=True):
        self.name, self._c = name, configured

    def configured(self):
        return self._c

    def ping(self):
        return True

    def spot_intraday(self, u):
        raise RuntimeError("down")


class TestFailoverUnconfigured(unittest.TestCase):
    def test_no_switch_to_unconfigured_secondary(self):
        alerts = []
        fo = FailoverManager(Prov("upstox"), Prov("dhan", configured=False), alert=alerts.append)
        for _ in range(5):
            with self.assertRaises(Exception):
                fo.call("spot_intraday", None)
        self.assertEqual(fo.active_name, "upstox")
        self.assertEqual(len([a for a in alerts if "no failover" in a]), 1)


class FakeBot:
    def __init__(self):
        self.last_tick, self.calls = {}, 0
        self.health_calls = 0
        self.health_probe_calls = 0
        self.diagnostics_calls = 0

    def health_probe(self):
        self.health_probe_calls += 1
        return {"ok": True, "time": "t", "mode": "loop+cron", "booted_for": None,
                "scanner": {"available": False, "stage": "not_initialized", "running": False}}

    def health(self):
        self.health_calls += 1
        return {"time": "t", "mode": "loop+cron", "booted_for": None, "provider": {"active": "upstox"}, "tokens": {}}

    def diagnostics(self):
        self.diagnostics_calls += 1
        return {"database": {"backend": "postgres", "persistent": True, "connected": True},
                "setup_modules": {"orb": False}, "universe": {"count": 1, "symbols": ["NIFTY"]},
                "last_scan": {"symbol_diagnostics": {"NIFTY": {"stage": "stage1", "reasons": ["test"]}}}}

    def tick(self, *a, **k):
        self.calls += 1
        return {"status": "ok"}


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import start_server
        cls.cfgctx = Cfg(CRON_SECRET="s3cret", WEBHOOK_SECRET="hook", UPSTOX_API_KEY="KEY", UPSTOX_ANALYTICS_TOKEN="", UPSTOX_ACCESS_TOKEN="")
        cls.cfgctx.__enter__()
        cls.bot = FakeBot()
        cls.srv = start_server(0, cls.bot)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.cfgctx.__exit__()

    def req(self, path, method="GET", body=None):
        r = urllib.request.Request(self.base + path, method=method, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw or b"{}")

    def test_health_public_and_head(self):
        code, health = self.req("/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])
        self.assertIn("scanner", health)
        self.assertEqual(self.bot.health_calls, 0)
        self.assertEqual(self.bot.health_probe_calls, 1)
        before = (self.bot.calls, self.bot.health_calls, self.bot.diagnostics_calls)
        req = urllib.request.Request(self.base + "/ping", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.read(), b"")
        self.assertEqual((self.bot.calls, self.bot.health_calls, self.bot.diagnostics_calls), before)
        version_code, version = self.req("/version")
        self.assertEqual(version_code, 200)
        self.assertEqual(version["service"], "nse-fno-signal-bot")
        self.assertIn("commit", version)
        self.assertEqual(self.req("/health", "HEAD")[0], 200)
        self.assertEqual(self.bot.health_calls, 0)

    def test_cron_requires_key(self):
        self.assertEqual(self.req("/cron/tick")[0], 403)
        self.assertEqual(self.req("/cron/tick?key=wrong")[0], 403)
        self.assertEqual(self.req("/status")[0], 403)
        self.assertEqual(self.req("/diagnostics")[0], 403)

    def test_diagnostics_protected_and_contains_details(self):
        self.assertEqual(self.req("/diagnostics?key=s3cret")[0], 200)
        code, data = self.req("/diagnostics?key=s3cret")
        self.assertEqual(code, 200)
        self.assertIn("setup_modules", data)
        self.assertIn("symbol_diagnostics", data["last_scan"])

    def test_cron_tick_accepted_and_runs(self):
        before = self.bot.calls
        code, j = self.req("/cron/tick?key=s3cret")
        self.assertEqual((code, j["accepted"]), (202, True))
        for _ in range(50):
            if self.bot.calls > before:
                break
            time.sleep(0.05)
        self.assertGreater(self.bot.calls, before)
        self.assertEqual(self.req("/cron/tick?key=s3cret&wait=1")[1], {"status": "ok"})

    def test_upstox_webhook(self):
        self.assertEqual(self.req("/webhook/upstox-token/nope", "POST", b"{}")[0], 403)
        body = json.dumps({"message_type": "access_token", "client_id": "KEY", "access_token": "WEBTOK",
                           "expires_at": str(int((time.time() + 3600) * 1000))}).encode()
        code, j = self.req("/webhook/upstox-token/hook", "POST", body)
        self.assertEqual((code, j["ok"]), (200, True))
        self.assertEqual(T.manager.upstox_token(), "WEBTOK")
        T.manager._up_runtime = ("", None)


class TestTickIdempotent(unittest.TestCase):
    def test_failed_scan_does_not_consume_candle_boundary(self):
        import bot as botmod
        from utils import market_hours as mh
        with mock.patch.object(botmod, "DB", make_test_db):
            b = botmod.Bot()
        today = date(2026, 10, 15)
        class Sc:
            last_stats = {}
            calls = 0
            def run_once(self, t):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary provider failure")
                return []
        scanner = Sc()
        b.boot_day, b.scanner, b.summary_day = today, scanner, today
        with mock.patch.object(botmod.tokens, "ensure_fresh"), mock.patch.object(mh, "is_trading_day", return_value=True):
            t = datetime(2026, 10, 15, 10, 0, 12, tzinfo=IST)
            self.assertEqual(b.tick(t, can_wait=False)["status"], "error")
            self.assertIsNone(b.last_scan_boundary)
            self.assertEqual(b.tick(t + timedelta(seconds=1), can_wait=False)["status"], "scanned")
        self.assertEqual(scanner.calls, 2)

    def test_missed_boundary_waits_toward_next_boundary(self):
        import bot as botmod
        from utils import market_hours as mh
        with mock.patch.object(botmod, "DB", make_test_db):
            b = botmod.Bot()
        today = date(2026, 10, 15)
        class Sc:
            last_stats = {}
            def run_once(self, t):
                raise AssertionError("stale candle must never be scanned")
        b.boot_day, b.scanner, b.summary_day = today, Sc(), today
        with mock.patch.object(botmod.tokens, "ensure_fresh"), mock.patch.object(mh, "is_trading_day", return_value=True):
            result = b.tick(datetime(2026, 10, 15, 10, 3, 30, tzinfo=IST), can_wait=False)
        self.assertEqual(result["status"], "missed_boundary")
        self.assertLessEqual(result["sleep"], 60)
        self.assertGreater(result["sleep"], 0)

    def test_one_scan_per_candle_boundary(self):
        import bot as botmod
        from utils import market_hours as mh
        with mock.patch.object(botmod, "DB", make_test_db):
            b = botmod.Bot()
        today = date(2026, 10, 15)                                  # a Thursday
        scans = []

        class Sc:
            last_stats = {}

            def run_once(self, t):
                scans.append(t)
                return []

        b.boot_day, b.scanner, b.summary_day = today, Sc(), today
        with mock.patch.object(botmod.tokens, "ensure_fresh"), mock.patch.object(mh, "is_trading_day", return_value=True):
            t1 = datetime(2026, 10, 15, 10, 0, 12, tzinfo=IST)
            self.assertEqual(b.tick(t1, can_wait=False)["status"], "scanned")
            self.assertEqual(b.tick(t1 + timedelta(seconds=30), can_wait=False)["status"], "waiting")   # same candle: no rescan
            self.assertEqual(b.tick(datetime(2026, 10, 15, 10, 5, 3, tzinfo=IST), can_wait=False)["status"], "waiting")  # not final yet
            self.assertEqual(b.tick(datetime(2026, 10, 15, 10, 5, 20, tzinfo=IST), can_wait=False)["status"], "scanned")
            self.assertEqual(b.tick(datetime(2026, 10, 15, 8, 0, tzinfo=IST))["status"], "idle")
            self.assertEqual(b.tick(datetime(2026, 10, 15, 8, 30, tzinfo=IST))["status"], "idle")
            self.assertEqual(b.tick(datetime(2026, 10, 15, 9, 10, 30, tzinfo=IST), can_wait=False)["status"], "no_scan_window")
        self.assertEqual(len(scans), 2)



class TestAsyncCandleWriter(unittest.TestCase):
    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_stalled_write_does_not_block_submit_and_reports_live_age(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowDB:
            def save_candles(self, symbol, candles):
                entered.set()
                release.wait(2.0)

        writer = AsyncCandleWriter(SlowDB)
        candle = Candle(datetime(2026, 11, 4, 10, 0), 100, 101, 99, 100, 1000, 10)
        started = time.monotonic()
        writer.submit("NIFTY", [candle])
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(entered.wait(1.0))
        time.sleep(0.03)
        state = writer.snapshot()
        self.assertEqual(state["active"]["symbol"], "NIFTY")
        self.assertGreaterEqual(state["active"]["elapsed_seconds"], 0.0)
        self.assertTrue(state["worker_alive"])
        release.set()
        self.assertTrue(self._wait_for(lambda: writer.snapshot()["written"] == 1))
        writer.close(timeout=1.0)

    def test_failed_write_is_reported_without_killing_writer(self):
        class FailingDB:
            def save_candles(self, symbol, candles):
                raise OSError("simulated database write failure")

        writer = AsyncCandleWriter(FailingDB)
        candle = Candle(datetime(2026, 11, 4, 10, 0), 100, 101, 99, 100, 1000, 10)
        writer.submit("NIFTY", [candle])
        self.assertTrue(self._wait_for(lambda: writer.snapshot()["failed"] == 1))
        state = writer.snapshot()
        self.assertIn("simulated database write failure", state["last_error"]["error"])
        writer.close(timeout=1.0)

    def test_successful_write_is_available_for_cache_recovery(self):
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        db = DB(connection=connection)
        writer = AsyncCandleWriter(lambda: db)
        candle = Candle(datetime(2026, 11, 4, 10, 0), 100, 101, 99, 100, 1000, 10)
        writer.submit("NIFTY", [candle])
        self.assertTrue(self._wait_for(lambda: writer.snapshot()["written"] == 1))
        recovered = db.candles_between("NIFTY", "2026-11-04", "2026-11-04")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].c, 100)
        writer.close(timeout=1.0)
        db.close()


if __name__ == "__main__":
    unittest.main()


class TestStage1FetchScheduling(unittest.TestCase):
    def test_results_are_processed_in_completion_order_not_symbol_order(self):
        symbols = [type("Symbol", (), {"name": name})() for name in ("SLOW", "FAST")]
        def fetch(symbol):
            time.sleep(0.08 if symbol.name == "SLOW" else 0.005)
            return (symbol.name, None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(_stage1_fetch_results(pool, symbols, fetch, 1.0))
        self.assertEqual([row[0].name for row in rows], ["FAST", "SLOW"])
        self.assertTrue(all(row[2] is None for row in rows))

    def test_slow_request_is_marked_timeout_without_holding_stage1(self):
        symbols = [type("Symbol", (), {"name": name})() for name in ("FAST", "STUCK")]
        def fetch(symbol):
            if symbol.name == "STUCK":
                time.sleep(0.60)
            return (symbol.name, None)
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            started = time.monotonic()
            rows = list(_stage1_fetch_results(pool, symbols, fetch, 0.05))
            elapsed = time.monotonic() - started
            by_name = {row[0].name: row for row in rows}
            self.assertLess(elapsed, 0.40)
            self.assertIsNone(by_name["FAST"][2])
            self.assertEqual(by_name["STUCK"][2], "timeout")
        finally:
            pool.shutdown(wait=True)

    def test_fetch_exception_is_reported_for_its_symbol(self):
        symbol = type("Symbol", (), {"name": "BROKEN"})()
        def fetch(_symbol):
            raise RuntimeError("provider unavailable")
        with ThreadPoolExecutor(max_workers=1) as pool:
            rows = list(_stage1_fetch_results(pool, [symbol], fetch, 1.0))
        self.assertEqual(rows[0][0].name, "BROKEN")
        self.assertIn("RuntimeError", rows[0][2])
