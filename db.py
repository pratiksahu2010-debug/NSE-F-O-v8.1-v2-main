"""PostgreSQL persistence (psycopg 3). No local-file fallback: production requires DATABASE_URL."""
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import List, Optional

from config import cfg
from utils.market_hours import now as market_now
from models import Candle, Signal

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY, ts TEXT, day TEXT, und TEXT, side TEXT, status TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_sig_day ON signals(day);
CREATE TABLE IF NOT EXISTS signal_updates(id {PK}, signal_id TEXT, ts TEXT, kind TEXT, premium {REAL}, note TEXT);
CREATE TABLE IF NOT EXISTS oi_history(ts TEXT, und TEXT, expiry TEXT, spot {REAL}, pcr {REAL}, call_oi {REAL}, put_oi {REAL}, call_wall {REAL}, put_wall {REAL}, atm_iv {REAL});
CREATE INDEX IF NOT EXISTS ix_oi ON oi_history(und, ts);
CREATE INDEX IF NOT EXISTS ix_oi_lookup ON oi_history(und, expiry, ts DESC);
CREATE TABLE IF NOT EXISTS iv_history(day TEXT, und TEXT, atm_iv {REAL}, PRIMARY KEY(day, und));
CREATE TABLE IF NOT EXISTS daily_stats(day TEXT PRIMARY KEY, signals INTEGER, wins INTEGER, losses INTEGER, pnl_lot {REAL});
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS telegram_outbox(
    id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL, signal_id TEXT NOT NULL,
    chat_id TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tg_outbox_pending ON telegram_outbox(status, created_at);
CREATE INDEX IF NOT EXISTS ix_tg_outbox_dedupe ON telegram_outbox(dedupe_key, status);
CREATE TABLE IF NOT EXISTS signal_features(signal_id TEXT PRIMARY KEY, day TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS candles(und TEXT, ts TEXT, o {REAL}, h {REAL}, l {REAL}, c {REAL}, v {REAL}, oi {REAL}, PRIMARY KEY(und, ts));
CREATE INDEX IF NOT EXISTS ix_sig_und_day ON signals(und, day);
CREATE INDEX IF NOT EXISTS ix_upd_sig ON signal_updates(signal_id);
CREATE TABLE IF NOT EXISTS rejection_events(
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT, stage TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_rejection_day ON rejection_events(day);
CREATE INDEX IF NOT EXISTS ix_rejection_symbol_day ON rejection_events(symbol, day);
"""


class DB:
    """Durable PostgreSQL storage (Render Postgres, Neon, Supabase, ...).

    SQL in this module is written with ``?`` placeholders; they are translated to psycopg's ``%s`` here, so the
    unit tests can still inject an in-memory SQLite connection (``connection=``) and run the same statements.
    """

    def __init__(self, path: str = None, url: str = None, connection=None, initialize_schema: bool = True):
        # `path` is kept for older tools: a postgres:// value is treated as the URL, anything else is ignored.
        if path and not url and str(path).startswith(("postgres://", "postgresql://")):
            url = path
        self.lock = threading.RLock()
        self.conn = connection
        self._url = self._normalize_url(url or cfg.DATABASE_URL)
        self._test_injected = connection is not None
        if self.conn is None:
            if not self._url:
                raise RuntimeError(
                    "PostgreSQL database is required. Set DATABASE_URL in the service environment "
                    "(on Render: attach a Postgres database and copy its Internal Database URL)."
                )
            try:
                self.conn = self._new_connection()
                probe = self._exec("SELECT 1")
                try:
                    probe.fetchone()
                    self.conn.commit()
                finally:
                    close = getattr(probe, "close", None)
                    if callable(close):
                        close()
            except Exception as exc:
                self._close_connection()
                raise RuntimeError(
                    f"Unable to connect to the configured PostgreSQL database: {type(exc).__name__}: {exc}"
                ) from exc

        if initialize_schema:
            if self._test_injected:
                schema = SCHEMA.format(PK="INTEGER PRIMARY KEY AUTOINCREMENT", REAL="REAL")
            else:
                schema = SCHEMA.format(PK="BIGSERIAL PRIMARY KEY", REAL="DOUBLE PRECISION")
            with self.lock:
                for stmt in [part.strip() for part in schema.split(";") if part.strip()]:
                    cursor = self._exec(stmt)
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
                self.conn.commit()

    # ---- connection management ----
    @staticmethod
    def _normalize_url(url: Optional[str]) -> str:
        """Render/Heroku hand out ``postgres://``; libpq accepts both, but SQLAlchemy-style prefixes are not valid."""
        url = (url or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
            if url.startswith(prefix):
                url = "postgresql://" + url[len(prefix):]
        return url

    def _new_connection(self):
        import psycopg
        from psycopg.rows import dict_row
        kwargs = dict(
            row_factory=dict_row,
            autocommit=False,
            connect_timeout=int(cfg.DB_CONNECT_TIMEOUT),
            application_name="nse-fno-bot",
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        )
        if int(cfg.DB_STATEMENT_TIMEOUT_MS) > 0:  # set to 0 for poolers (e.g. Neon/pgbouncer) that reject startup options
            kwargs["options"] = f"-c statement_timeout={int(cfg.DB_STATEMENT_TIMEOUT_MS)}"
        return psycopg.connect(self._url, **kwargs)

    def _close_connection(self):
        old, self.conn = self.conn, None
        if old is not None:
            try:
                old.close()
            except Exception:
                log.debug("Ignoring error while closing stale database connection", exc_info=True)

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """True only for dropped/broken PostgreSQL connections (idle timeout, restart, failover); never for SQL errors."""
        for cls in type(exc).__mro__:
            if cls.__module__.startswith("psycopg") and cls.__name__ in ("OperationalError", "InterfaceError"):
                return True
        return False

    def _reconnect(self):
        """Replace a dead connection with a brand-new one."""
        if self._test_injected:
            raise RuntimeError("Cannot reconnect an explicitly injected test database connection")
        self._close_connection()
        self.conn = self._new_connection()
        log.warning("Database connection was lost; established a fresh PostgreSQL connection")

    def _rollback(self):
        try:
            if self.conn is not None:
                self.conn.rollback()
        except Exception:
            log.debug("rollback failed", exc_info=True)

    def close(self):
        """Close the database connection cleanly during process shutdown."""
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                finally:
                    self.conn = None

    @property
    def backend(self) -> str:
        return "test-injected" if self._test_injected else "postgres"

    # ---- DB-API plumbing ----
    def _adapt(self, sql: str) -> str:
        if self._test_injected:
            return sql
        # psycopg uses %s placeholders and treats a bare % as one, so literal % (LIKE 'alert:%') must be doubled.
        return sql.replace("%", "%%").replace("?", "%s")

    def _exec(self, sql: str, args=()):
        return self.conn.execute(self._adapt(sql), tuple(args or ()))

    @staticmethod
    def _mapping_rows(cursor, rows):
        if not rows:
            return []
        description = getattr(cursor, "description", None) or ()
        columns = [col[0] for col in description]
        mapped = []
        for row in rows:
            if isinstance(row, dict):
                mapped.append(row)
                continue
            if hasattr(row, "keys"):
                mapped.append({key: row[key] for key in row.keys()})
                continue
            mapped.append(dict(zip(columns, row)))
        return mapped

    def _run(self, sql: str, args=(), fetch=False):
        with self.lock:
            for attempt in range(2):
                if self.conn is None:
                    if self._test_injected:
                        raise RuntimeError("Database connection is closed")
                    self._reconnect()
                cursor = None
                try:
                    cursor = self._exec(sql, args)
                    rows = self._mapping_rows(cursor, cursor.fetchall()) if fetch else None
                    self.conn.commit()
                    return rows
                except Exception as exc:
                    self._rollback()  # a failed statement leaves PostgreSQL's transaction aborted
                    if attempt == 0 and not self._test_injected and self._is_connection_error(exc):
                        log.warning("Database connection error (%s); reconnecting and retrying once", type(exc).__name__)
                        self._reconnect()
                        continue
                    raise
                finally:
                    if cursor is not None:
                        close = getattr(cursor, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                log.debug("Ignoring cursor close failure", exc_info=True)
            raise RuntimeError("Database operation exhausted its single retry")

    def _many(self, sql: str, rows):
        if not rows:
            return
        with self.lock:
            for attempt in range(2):
                if self.conn is None:
                    if self._test_injected:
                        raise RuntimeError("Database connection is closed")
                    self._reconnect()
                cursor = None
                try:
                    cursor = self.conn.cursor()
                    cursor.executemany(self._adapt(sql), rows)
                    self.conn.commit()
                    return
                except Exception as exc:
                    self._rollback()
                    if attempt == 0 and not self._test_injected and self._is_connection_error(exc):
                        log.warning("Database connection error during batch insert; reconnecting and retrying once")
                        self._reconnect()
                        continue
                    raise
                finally:
                    if cursor is not None:
                        close = getattr(cursor, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                log.debug("Ignoring cursor close failure", exc_info=True)
            raise RuntimeError("Database batch operation exhausted its single retry")

    def _x(self, sql, args=()):
        self._run(sql, args)

    def _q(self, sql, args=()):
        return self._run(sql, args, fetch=True)

    # ---- key/value (state that must survive restarts) ----
    def kv_get(self, k: str, default=None):
        r = self._q("SELECT v FROM kv WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def kv_set(self, k: str, v: str):
        self._x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---- durable Telegram signal outbox ----
    def enqueue_telegram(self, dedupe_key: str, signal_id: str, chat_id: str, message: str):
        """Create one durable delivery record per configured chat; retries never resend to chats already acknowledged."""
        # Store each Telegram chunk as its own delivery row. If chunk 3 fails,
        # chunks 1 and 2 remain DELIVERED and are never resent on retry.
        chunks = [message[i:i + 3900] for i in range(0, len(message), 3900)] or [""]
        for index, chunk in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f":{index:04d}"
            row_id = f"{dedupe_key}:{chat_id}{suffix}"
            self._x(
                "INSERT INTO telegram_outbox(id,dedupe_key,signal_id,chat_id,message,status,attempts,created_at) "
                "VALUES(?,?,?,?,?,'PENDING',0,?) ON CONFLICT(id) DO NOTHING",
                (row_id, dedupe_key, signal_id, str(chat_id), chunk,
                 market_now().isoformat(timespec="seconds")),
            )

    def pending_telegram(self, limit: int = 100) -> list:
        rows = self._q(
            "SELECT id,dedupe_key,signal_id,chat_id,message,attempts FROM telegram_outbox "
            "WHERE status='PENDING' ORDER BY created_at LIMIT ?", (max(1, int(limit)),)
        )
        return [dict(r) for r in rows]

    def mark_telegram_delivered(self, row_id: str):
        self._x("UPDATE telegram_outbox SET status='DELIVERED',delivered_at=?,last_error=NULL WHERE id=?",
                (market_now().isoformat(timespec="seconds"), row_id))

    def mark_telegram_failed(self, row_id: str, error: str):
        self._x("UPDATE telegram_outbox SET attempts=attempts+1,last_error=? WHERE id=?",
                (str(error or "delivery failed")[:500], row_id))

    def telegram_delivery_status(self, dedupe_key: str) -> dict:
        rows = self._q("SELECT status,COUNT(*) AS n FROM telegram_outbox WHERE dedupe_key=? GROUP BY status",
                       (dedupe_key,))
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        total = sum(counts.values())
        return {"total": total, "delivered": counts.get("DELIVERED", 0),
                "pending": counts.get("PENDING", 0), "all_delivered": total > 0 and counts.get("DELIVERED", 0) == total}

    def telegram_outbox_summary(self) -> dict:
        rows = self._q("SELECT status,COUNT(*) AS n FROM telegram_outbox GROUP BY status")
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        latest = self._q("SELECT last_error FROM telegram_outbox WHERE status='PENDING' AND last_error IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT 1")
        return {"pending": counts.get("PENDING", 0), "delivered": counts.get("DELIVERED", 0),
                "last_error": latest[0]["last_error"] if latest else None}

    # ---- signals ----
    def save_signal(self, s: Signal):
        self._x("INSERT INTO signals(id,ts,day,und,side,status,data) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, day=excluded.day, und=excluded.und, side=excluded.side, "
                "status=excluded.status, data=excluded.data",
                (s.id, s.ts, s.ts[:10], s.underlying, s.side, s.status, json.dumps(s.to_dict())))

    def _load(self, rows) -> List[Signal]:
        out = []
        for r in rows:
            d = json.loads(r["data"])
            out.append(Signal(**{k: v for k, v in d.items() if k in Signal.__dataclass_fields__}))
        return out

    def closed_since(self, day: str) -> List[Signal]:
        """Closed signals from `day` (YYYY-MM-DD) onwards, oldest first (for the adaptive layer and the report)."""
        return self._load(self._q("SELECT data FROM signals WHERE day>=? AND status='CLOSED' ORDER BY ts", (day,)))

    def active_signals(self, day: str) -> List[Signal]:
        return self._load(self._q("SELECT data FROM signals WHERE day=? AND status IN ('ACTIVE','T1_HIT')", (day,)))

    def signals_on(self, day: str) -> List[Signal]:
        return self._load(self._q("SELECT data FROM signals WHERE day=? ORDER BY ts", (day,)))

    def last_signal_ts(self, und: str, day: str) -> Optional[str]:
        r = self._q("SELECT MAX(ts) AS m FROM signals WHERE und=? AND day=?", (und, day))
        return r[0]["m"] if r and r[0]["m"] else None

    # ---- per-signal features (calibration) and candle archive (replay) ----
    def save_features(self, signal_id: str, day: str, data: dict):
        self._x("INSERT INTO signal_features(signal_id,day,data) VALUES(?,?,?) "
                "ON CONFLICT(signal_id) DO UPDATE SET day=excluded.day, data=excluded.data",
                (signal_id, day, json.dumps(data, default=str)))

    def features_since(self, day: str) -> dict:
        rows = self._q("SELECT signal_id, data FROM signal_features WHERE day>=?", (day,))
        return {r["signal_id"]: json.loads(r["data"]) for r in rows}

    def save_candles(self, und: str, candles: List[Candle]):
        if not candles:
            return
        rows = [
            (und, c.ts.isoformat(), c.o, c.h, c.l, c.c, c.v, c.oi)
            for c in candles
        ]
        self._many(
            "INSERT INTO candles(und,ts,o,h,l,c,v,oi) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(und,ts) DO NOTHING",
            rows,
        )

    def candle_symbols(self) -> List[str]:
        return [r["und"] for r in self._q("SELECT DISTINCT und FROM candles ORDER BY und")]

    def candles_between(self, und: str, start_day: str, end_day: str) -> List[Candle]:
        rows = self._q("SELECT ts,o,h,l,c,v,oi FROM candles WHERE und=? AND substr(ts,1,10)>=? AND substr(ts,1,10)<=? ORDER BY ts",
                       (und, start_day, end_day))
        return [Candle(datetime.fromisoformat(r["ts"]), r["o"], r["h"], r["l"], r["c"], r["v"], r["oi"]) for r in rows]

    def add_update(self, signal_id: str, kind: str, premium: float, note: str = ""):
        self._x("INSERT INTO signal_updates(signal_id,ts,kind,premium,note) VALUES(?,?,?,?,?)",
                (signal_id, market_now().isoformat(timespec="seconds"), kind, premium, note))

    # ---- durable rejection and activity accounting ----
    def record_rejection(self, event_id: str, ts: str, day: str, symbol: str,
                         side: str, stage: str, reason: str, detail: str = ""):
        """Persist one deduplicated rejected candidate; event_id is stable within a scan cycle."""
        self._x(
            "INSERT INTO rejection_events(id,ts,day,symbol,side,stage,reason,detail) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (str(event_id), str(ts), str(day), str(symbol or ""), str(side or ""),
             str(stage or "unknown"), str(reason or "unknown"), str(detail or "")[:500]),
        )

    def activity_summary(self, day: str) -> dict:
        """Separate generated/delivered signals, SL/target outcomes and rejected setups."""
        signal_rows = self._q("SELECT status,data FROM signals WHERE day=?", (day,))
        by_strategy = {}
        by_symbol = {}
        for row in signal_rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                data = {}
            strategy = str(data.get("strategy") or "UNKNOWN")
            symbol = str(data.get("underlying") or "")
            by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
            if symbol:
                by_symbol[symbol] = by_symbol.get(symbol, 0) + 1

        update_rows = self._q(
            "SELECT u.kind,COUNT(*) AS n FROM signal_updates u "
            "JOIN signals s ON s.id=u.signal_id WHERE s.day=? GROUP BY u.kind",
            (day,),
        )
        updates = {str(row["kind"]).upper(): int(row["n"]) for row in update_rows}
        delivery_rows = self._q(
            "SELECT o.status,COUNT(*) AS n FROM telegram_outbox o "
            "JOIN signals s ON s.id=o.signal_id WHERE s.day=? GROUP BY o.status",
            (day,),
        )
        delivery = {str(row["status"]).upper(): int(row["n"]) for row in delivery_rows}
        reject_rows = self._q(
            "SELECT reason,COUNT(*) AS n FROM rejection_events WHERE day=? GROUP BY reason ORDER BY n DESC",
            (day,),
        )
        reject_symbols = self._q(
            "SELECT symbol,COUNT(*) AS n FROM rejection_events WHERE day=? GROUP BY symbol ORDER BY n DESC",
            (day,),
        )
        rejected = sum(int(row["n"]) for row in reject_rows)
        return {
            "day": day,
            "signals_generated": len(signal_rows),
            "signals_by_strategy": dict(sorted(by_strategy.items())),
            "signals_by_symbol": dict(sorted(by_symbol.items())),
            "telegram_deliveries": {"delivered": delivery.get("DELIVERED", 0),
                                    "pending": delivery.get("PENDING", 0),
                                    "failed_attempts": delivery.get("FAILED", 0)},
            "outcomes": {"stop_loss_hits": updates.get("SL", 0) + updates.get("UND_SL", 0),
                         "target1_hits": updates.get("T1", 0), "target2_hits": updates.get("T2", 0),
                         "time_exits": updates.get("TIME", 0), "all_update_events": updates},
            "rejected_setups": {"total": rejected,
                                "by_reason": {str(row["reason"]): int(row["n"]) for row in reject_rows},
                                "by_symbol": {str(row["symbol"]): int(row["n"]) for row in reject_symbols}},
        }

    # ---- OI snapshots ----
    def save_oi(self, ts: str, und: str, expiry: str, spot: float, pcr: float, call_oi: float, put_oi: float,
                call_wall: float, put_wall: float, atm_iv: float):
        self._x("INSERT INTO oi_history(ts,und,expiry,spot,pcr,call_oi,put_oi,call_wall,put_wall,atm_iv) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, und, expiry, spot, pcr, call_oi, put_oi, call_wall, put_wall, atm_iv))

    def oi_before(self, und: str, expiry: str, before_ts: str) -> Optional[dict]:
        r = self._q("SELECT * FROM oi_history WHERE und=? AND expiry=? AND ts<=? ORDER BY ts DESC LIMIT 1", (und, expiry, before_ts))
        if not r:
            return None
        r = r[0]
        return {"pcr": r["pcr"], "call_wall": r["call_wall"], "put_wall": r["put_wall"], "ts": r["ts"]}

    def save_iv(self, day: str, und: str, iv: float):
        if iv > 0:
            self._x("INSERT INTO iv_history(day,und,atm_iv) VALUES(?,?,?) ON CONFLICT(day,und) DO UPDATE SET atm_iv=excluded.atm_iv",
                    (day, und, iv))

    def iv_hist(self, und: str, n: int = 250) -> List[float]:
        return [r["atm_iv"] for r in self._q("SELECT atm_iv FROM iv_history WHERE und=? ORDER BY day DESC LIMIT ?", (und, n))]

    def save_daily(self, day: str, signals: int, wins: int, losses: int, pnl: float):
        self._x("INSERT INTO daily_stats(day,signals,wins,losses,pnl_lot) VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
                "signals=excluded.signals, wins=excluded.wins, losses=excluded.losses, pnl_lot=excluded.pnl_lot",
                (day, signals, wins, losses, pnl))

    def purge(self, keep_days: int = 5):
        """OI snapshots are only compared ~30 min apart; keep a few days, not forever (free DB tiers are small)."""
        cutoff = (market_now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
        self._x("DELETE FROM oi_history WHERE ts<?", (cutoff,))
        day_cutoff = (market_now() - timedelta(days=keep_days)).date().isoformat()
        # Alert de-dup keys are per day ("alert:<YYYY-MM-DD>:..."); signal detail rows follow their signal's day.
        self._x("DELETE FROM kv WHERE k LIKE 'alert:%' AND substr(k, 7, 10) < ?", (day_cutoff,))
        candle_cutoff = (market_now() - timedelta(days=cfg.CANDLE_KEEP_DAYS)).date().isoformat()
        self._x("DELETE FROM candles WHERE substr(ts,1,10) < ?", (candle_cutoff,))
        self._x("DELETE FROM signal_updates WHERE signal_id IN (SELECT id FROM signals WHERE day < ?)", (day_cutoff,))
        rejection_cutoff = (market_now() - timedelta(days=30)).date().isoformat()
        self._x("DELETE FROM rejection_events WHERE day < ?", (rejection_cutoff,))
