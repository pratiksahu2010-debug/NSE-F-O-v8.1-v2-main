import html
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, time as dtime, timedelta
from typing import Dict, List

from auth.tokens import manager as tokens
from config import cfg
from engine.analytics import strategy_performance
from data.upstox_only import UpstoxOnlyProvider
from data.instruments import Master, build_underlyings, download_master
from data.upstox_client import UpstoxProvider
from data.upstox_ws import UpstoxWS
from db import DB
from data.history_persistence import AsyncCandleWriter
from engine.report import build as build_report, format_report
from engine.risk_rules import RiskRules
from engine.scanner import Scanner
from engine.tracker import Tracker
from models import Candle, Underlying
from telegram_service import Telegram
from utils import market_hours as mh
from strategies.mex_universe import MEX_SYMBOLS
from strategies.orb_universe import ORB_SYMBOLS
from strategies.normal_universe import NORMAL_SYMBOLS

log = logging.getLogger("bot")


class Bot:
    def __init__(self):
        self.tg = Telegram()
        self.db = DB()
        # Candle archive writes use a dedicated database connection and daemon worker.
        # A slow remote write must never hold the scanner's primary DB lock.
        self.history_writer = AsyncCandleWriter(lambda: DB(initialize_schema=False))
        self.ws = UpstoxWS()
        self.ws.on_tick = self._on_ws_tick
        tokens.alert = self.tg.send
        # Token persistence gets its own lazy database client. Sharing self.db here
        # would let a stalled background token read hold the primary DB lock and
        # block daily bootstrap/cache access even though restore runs in a thread.
        tokens.kv = None
        tokens.kv_factory = lambda: DB(initialize_schema=False)
        tokens._kv_client = None
        tokens.on_change = lambda which: self.ws.reconnect() if which == "upstox" else None
        self.upstox = UpstoxProvider(ws=self.ws)
        # Explicitly Upstox-only: provider errors surface to diagnostics; no hidden broker failover.
        self.fo = UpstoxOnlyProvider(self.upstox)
        self.risk = RiskRules(self.db)
        self.unds: List[Underlying] = []
        self.unds_by_name: Dict[str, Underlying] = {}
        self.tracker = Tracker(self.db, self.tg, self.fo, self.ws, self.unds_by_name)
        self.scanner = None
        self.boot_day = None
        self.summary_day = None
        self.started = False
        self.master = None
        self.universe_diagnostics = {"mode": "not_bootstrapped", "unique_stock_union": []}
        self.history_bootstrap_diagnostics = {"status": "not_started", "symbols": 0, "from_cache": 0,
                                              "from_upstox": 0, "cache_fallback": 0, "cache_write_failed": 0, "failed": 0,
                                              "short_history": 0, "min_bars": 0, "max_bars": 0,
                                              "errors": {},
                                              "active_symbols": {}, "last_symbol": None,
                                              "last_progress_at": None}
        self.last_scan_boundary = None
        self._last_missed_boundary = None
        self.day_checked = None
        self.closed_day = None
        self._holiday_empty_warn_at = 0.0
        self.last_tick = {}
        self._boot_fail = 0.0
        self._tick_lock = threading.Lock()
        self._tick_stage = "idle"
        self._tick_stage_started = None
        self._last_tick_finished_at = None
        self._last_tick_result = None
        self._boot_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._bootstrap_notice_lock = threading.Lock()
        self._bootstrap_notice_thread = None
        self._bootstrap_notice_status = {"state": "not_started", "started_at": None, "finished_at": None, "error": None}
        self._holiday_check_lock = threading.Lock()
        self._holiday_check_thread = None
        self._holiday_check_status = {"state": "idle", "date": None, "started_at": None, "finished_at": None, "error": None}

    def _on_ws_tick(self, instrument_key, tick, received_at):
        """Feed decoded Upstox ticks to the scanner without blocking the websocket loop."""
        scanner = self.scanner
        if scanner is None or not self.ws.enabled:
            return
        current = mh.now()
        if self.closed_day == current.date() or not mh.is_trading_day(current.date()) or not mh.can_signal(current):
            return
        try:
            scanner.ingest_live_tick(instrument_key, tick, received_at)
        except Exception:
            log.exception("live tick ingestion failed for %s", instrument_key)

    def _send_bootstrap_notices(self, messages):
        """Deliver optional startup notices away from the scanner control path."""
        with self._bootstrap_notice_lock:
            thread = self._bootstrap_notice_thread
            if thread is not None and thread.is_alive():
                log.info("bootstrap notifications already running; skipping duplicate batch")
                return
            started_at = mh.now().isoformat(timespec="seconds")
            self._bootstrap_notice_status = {"state": "running", "started_at": started_at, "finished_at": None, "error": None}

            def _deliver():
                errors = []
                for message in messages:
                    try:
                        if self.tg.send(message) is False:
                            errors.append(getattr(self.tg, "last_error", None) or "Telegram did not acknowledge")
                    except Exception as exc:
                        errors.append(f"{type(exc).__name__}: {exc}")
                        log.exception("bootstrap Telegram notice failed")
                with self._bootstrap_notice_lock:
                    self._bootstrap_notice_status = {
                        "state": "failed" if errors else "sent",
                        "started_at": started_at,
                        "finished_at": mh.now().isoformat(timespec="seconds"),
                        "error": "; ".join(errors)[:300] if errors else None,
                    }

            self._bootstrap_notice_thread = threading.Thread(
                target=_deliver, name="bootstrap-notifications", daemon=True
            )
            self._bootstrap_notice_thread.start()

    # ---------- daily bootstrap ----------
    def bootstrap(self, today: date):
        with self._boot_lock:
            if self.boot_day == today:
                return
            self._bootstrap(today)

    def _bootstrap(self, today: date):
        log.info("Bootstrapping for %s", today)
        tokens.ensure_fresh(morning=True)
        try:
            self.db.purge()
        except Exception:
            log.exception("db purge failed")
        master = Master(download_master())
        self.master = master
        everything = build_underlyings(master, today)
        indices = [u for u in everything if u.kind == "INDEX"]
        stocks = [u for u in everything if u.kind == "STOCK"]

        by = {u.name: u for u in stocks}
        # Fixed universes replace dynamic turnover ranking and additive priority expansion.
        # The current Upstox NSE_FO master remains the final eligibility authority.
        normal_requested = list(NORMAL_SYMBOLS)
        mex_requested = list(MEX_SYMBOLS)
        orb_requested = list(ORB_SYMBOLS)
        requested = list(dict.fromkeys(normal_requested + mex_requested + orb_requested))
        normal_in_master = [name for name in normal_requested if name in by]
        mex_in_master = [name for name in mex_requested if name in by]
        orb_in_master = [name for name in orb_requested if name in by]
        missing_normal = [name for name in normal_requested if name not in by]
        missing_mex = [name for name in mex_requested if name not in by]
        missing_orb = [name for name in orb_requested if name not in by]
        selected_stocks = [by[name] for name in requested if name in by]
        self.universe_diagnostics = {
            "mode": "fixed_static",
            "normal_requested_count": len(normal_requested),
            "normal_in_master": normal_in_master,
            "normal_missing_from_master": missing_normal,
            "mex_requested_count": len(mex_requested),
            "mex_in_master": mex_in_master,
            "mex_missing_from_master": missing_mex,
            "orb_requested_count": len(orb_requested),
            "orb_in_master": orb_in_master,
            "orb_missing_from_master": missing_orb,
            "unique_stock_union": [u.name for u in selected_stocks],
            "normal_symbols": normal_in_master,
            "mex_symbols": mex_in_master,
            "orb_symbols": orb_in_master,
        }
        self.unds[:] = indices + selected_stocks
        self.unds_by_name.clear()
        self.unds_by_name.update({u.name: u for u in self.unds})
        selected_indices = [u for u in self.unds if u.kind == "INDEX"]
        selected_stocks = [u for u in self.unds if u.kind == "STOCK"]
        log.info(
            "Fixed universe selected | indices=%d | normal=%d/%d | momentum_explosion=%d/%d | unique_stocks=%d | total=%d",
            len(selected_indices), len(normal_in_master), len(normal_requested),
            len(mex_in_master), len(mex_requested), len(selected_stocks), len(self.unds),
        )
        log.info("Universe indices: %s", ",".join(u.name for u in selected_indices))
        log.info("Normal fixed symbols: %s", ",".join(normal_in_master))
        log.info("Momentum Explosion fixed symbols: %s", ",".join(mex_in_master))
        log.info("Opening Range Breakout fixed symbols: %d/%d | last_entry=%02d:%02d | symbols=%s",
                 len(orb_in_master), len(orb_requested), cfg.ORB_LAST_TIME[0], cfg.ORB_LAST_TIME[1],
                 ",".join(orb_in_master))

        history_diag_lock = threading.RLock()
        hist_min_bars = int(cfg.SIMPLE_MIN_BARS) if cfg.STRATEGY_MODE != "LEGACY" else int(cfg.V7_MIN_BARS)

        def _hist(u):
            symbol_started = time.monotonic()
            stage = "queued"
            outcome = None

            stage_started = symbol_started

            def _set_stage(value):
                nonlocal stage, stage_started
                stage = value
                stage_started = time.monotonic()
                with history_diag_lock:
                    active = dict(self.history_bootstrap_diagnostics.get("active_symbols") or {})
                    active[u.name] = {
                        "stage": stage,
                        "stage_started_monotonic": stage_started,
                        "elapsed_seconds": 0.0,
                        "total_elapsed_seconds": round(stage_started - symbol_started, 1),
                    }
                    self.history_bootstrap_diagnostics["active_symbols"] = active
                    self.history_bootstrap_diagnostics["last_progress_at"] = mh.now().isoformat(timespec="seconds")

            _set_stage("db_cache_read")
            try:
                # Cache reads are synchronous. Fetched candles are queued to an
                # isolated writer so database latency cannot block scanner startup.
                # Completed writes remain durable per-symbol checkpoints; symbols whose
                # writes are interrupted by a process restart are fetched again next boot.
                end_day = today - timedelta(days=1)
                start_day = today - timedelta(days=max(cfg.HIST_DAYS + 10, 20))
                cached = []
                cache_read_failed = False
                try:
                    cached = self.db.candles_between(u.name, start_day.isoformat(), end_day.isoformat())
                except Exception:
                    cache_read_failed = True
                    log.exception("historical candle cache read failed for %s", u.name)
                cached = sorted({c.ts: c for c in cached}.values(), key=lambda c: c.ts)
                cache_recent = bool(cached and cached[-1].ts.date() >= end_day - timedelta(days=7))
                if len(cached) >= hist_min_bars and cache_recent:
                    outcome = (u.name, cached, "cache", None)
                    return outcome

                _set_stage("upstox_history")
                try:
                    fetched = self.fo.call("history", u, cfg.HIST_DAYS)
                    merged = {c.ts: c for c in cached}
                    merged.update({c.ts: c for c in fetched})
                    result = sorted(merged.values(), key=lambda c: c.ts)
                    persistence_error = None
                    if fetched:
                        _set_stage("db_cache_write_queue")
                        try:
                            self.history_writer.submit(u.name, fetched)
                        except Exception as exc:
                            persistence_error = f"candle cache enqueue failed: {type(exc).__name__}: {exc}"[:180]
                            log.exception("historical candle cache enqueue failed for %s", u.name)
                    if len(result) < hist_min_bars:
                        log.warning("historical warmup short for %s | api=%d cached=%d total=%d required=%d",
                                    u.name, len(fetched), len(cached), len(result), hist_min_bars)
                    source = ("upstox_unpersisted" if persistence_error else "upstox_queued") if fetched else ("cache_fallback" if cached else "empty")
                    error = persistence_error or ("database candle cache read failed" if cache_read_failed else None)
                    outcome = (u.name, result, source, error)
                    return outcome
                except Exception as e:
                    log.warning("history failed for %s: %s | cached_fallback=%d",
                                u.name, e, len(cached))
                    outcome = (u.name, cached, "cache_fallback" if cached else "failed", str(e)[:180])
                    return outcome
            finally:
                elapsed = round(time.monotonic() - symbol_started, 1)
                with history_diag_lock:
                    active = dict(self.history_bootstrap_diagnostics.get("active_symbols") or {})
                    active.pop(u.name, None)
                    self.history_bootstrap_diagnostics["active_symbols"] = active
                    self.history_bootstrap_diagnostics["last_symbol"] = {
                        "symbol": u.name,
                        "stage": stage,
                        "source": outcome[2] if outcome else "unknown",
                        "bars": len(outcome[1]) if outcome else 0,
                        "elapsed_seconds": elapsed,
                    }
                    self.history_bootstrap_diagnostics["last_progress_at"] = mh.now().isoformat(timespec="seconds")
                if elapsed >= 15.0:
                    log.warning("history symbol completed slowly | symbol=%s | final_stage=%s | source=%s | elapsed=%.1fs",
                                u.name, stage, outcome[2] if outcome else "unknown", elapsed)

        # The provider limiter paces requests globally. Collect completed futures
        # as they finish so slow symbols cannot hide bootstrap progress in logs.
        bootstrap_started = time.monotonic()
        bootstrap_started_at = mh.now().isoformat(timespec="seconds")
        self.history_bootstrap_diagnostics = {
            "status": "running", "stage": "historical_warmup",
            "started_at": bootstrap_started_at, "completed_symbols": 0,
            "symbols": len(self.unds), "from_cache": 0, "from_upstox": 0,
            "cache_fallback": 0, "failed": 0, "short_history": 0,
            "required_bars": hist_min_bars, "min_bars": 0, "max_bars": 0,
            "elapsed_seconds": 0.0, "errors": {}, "active_symbols": {},
            "last_symbol": None, "last_progress_at": bootstrap_started_at,
        }
        history_results = []
        worker_count = max(1, min(2, cfg.SCAN_WORKERS))
        log.info("history bootstrap workers started | workers=%d | symbols=%d | persistence=PostgreSQL per symbol",
                 worker_count, len(self.unds))
        with ThreadPoolExecutor(max_workers=worker_count) as ex:
            futures = {ex.submit(_hist, u): u.name for u in self.unds}
            pending = set(futures)
            completed = 0
            while pending:
                done, pending = wait(pending, timeout=20.0, return_when=FIRST_COMPLETED)
                if not done:
                    now_mono = time.monotonic()
                    with history_diag_lock:
                        active = {
                            name: dict(item)
                            for name, item in (self.history_bootstrap_diagnostics.get("active_symbols") or {}).items()
                        }
                    elapsed = round(now_mono - bootstrap_started, 1)
                    active_display = {}
                    for name, item in active.items():
                        stage_started_at = item.get("stage_started_monotonic", now_mono)
                        item["elapsed_seconds"] = round(max(0.0, now_mono - stage_started_at), 1)
                        item["total_elapsed_seconds"] = round(
                            max(0.0, now_mono - (stage_started_at - item.get("total_elapsed_seconds", 0.0))), 1
                        )
                        active_display[name] = {
                            "stage": item.get("stage"),
                            "elapsed_seconds": item["elapsed_seconds"],
                            "total_elapsed_seconds": item["total_elapsed_seconds"],
                        }
                    writer_state = self.history_writer.snapshot()
                    active_summary = ";".join(
                        f"{name}:{item.get('stage')}:{item.get('elapsed_seconds')}s"
                        for name, item in list(active_display.items())[:4]
                    ) or "no active symbol reported"
                    writer_active = (
                        f"{writer_state['active']['symbol']}:{writer_state['active']['elapsed_seconds']}s"
                        if writer_state["active"] else "none"
                    )
                    log.warning(
                        "history bootstrap waiting | completed=%d/%d | active=%s | elapsed=%.1fs | candle_writer_pending=%d | candle_writer_active=%s",
                        completed, len(futures), active_summary, elapsed, writer_state["pending"], writer_active,
                    )
                    self.history_bootstrap_diagnostics.update({
                        "completed_symbols": completed, "elapsed_seconds": elapsed,
                        "active_symbols": active,
                    })
                    continue
                for future in done:
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = (futures[future], [], "failed", str(exc)[:180])
                    history_results.append(result)
                    completed += 1
                if completed % 10 == 0 or completed == len(futures):
                    elapsed = round(time.monotonic() - bootstrap_started, 1)
                    sources = [item[2] for item in history_results]
                    writer_state = self.history_writer.snapshot()
                    log.info(
                        "history bootstrap progress | completed=%d/%d | cache=%d | upstox=%d | fallback=%d | failed=%d | candle_writes_queued=%d | candle_writes_pending=%d | candle_writes_failed=%d | elapsed=%.1fs",
                        completed, len(futures), sources.count("cache"),
                        sources.count("upstox") + sources.count("upstox_queued"),
                        sources.count("cache_fallback"), sources.count("failed") + sources.count("empty"),
                        writer_state["queued"], writer_state["pending"], writer_state["failed"], elapsed,
                    )
                    self.history_bootstrap_diagnostics.update({
                        "completed_symbols": completed, "from_cache": sources.count("cache"),
                        "from_upstox": sources.count("upstox") + sources.count("upstox_queued"),
                        "cache_fallback": sources.count("cache_fallback"),
                        "cache_write_failed": sources.count("upstox_unpersisted"),
                        "cache_write_queued": sources.count("upstox_queued"),
                        "persistence": writer_state,
                        "failed": sources.count("failed") + sources.count("empty"),
                        "elapsed_seconds": elapsed,
                    })
        hist: Dict[str, List[Candle]] = {name: candles for name, candles, _, _ in history_results}
        history_sources = [source for _, _, source, _ in history_results]
        history_errors = {name: error for name, _, _, error in history_results if error}
        thin = [n for n, h in hist.items() if len(h) < hist_min_bars]
        bar_counts = [len(h) for h in hist.values()]
        self.history_bootstrap_diagnostics = {
            "status": "ready" if not thin else "incomplete",
            "stage": "completed", "started_at": bootstrap_started_at,
            "completed_at": mh.now().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.monotonic() - bootstrap_started, 1),
            "completed_symbols": len(hist), "symbols": len(hist),
            "from_cache": history_sources.count("cache"),
            "from_upstox": history_sources.count("upstox") + history_sources.count("upstox_queued"),
            "cache_fallback": history_sources.count("cache_fallback"),
            "cache_write_failed": history_sources.count("upstox_unpersisted"),
            "cache_write_queued": history_sources.count("upstox_queued"),
            "persistence": self.history_writer.snapshot(),
            "failed": history_sources.count("failed") + history_sources.count("empty"),
            "short_history": len(thin),
            "required_bars": hist_min_bars,
            "min_bars": min(bar_counts, default=0),
            "max_bars": max(bar_counts, default=0),
            "errors": history_errors,
            "active_symbols": {},
            "last_symbol": self.history_bootstrap_diagnostics.get("last_symbol"),
            "last_progress_at": mh.now().isoformat(timespec="seconds"),
        }
        if thin:
            # This is the classic "candle warmup forever" failure: without 5m history the indicators cannot warm up.
            log.warning("history missing/short for %d of %d symbols: %s", len(thin), len(hist), ",".join(thin[:10]))
            self.tg.send(f"⚠️ 5m history is missing for {len(thin)}/{len(hist)} symbols (e.g. {', '.join(thin[:5])}). "
                         f"The scanner needs at least {hist_min_bars} completed 5m candles for EMA, VWAP and ADX warmup; "
                         f"it will keep retrying history during the session. "
                         "Check the data token / provider (see /diagnostics).")

        self.scanner = Scanner(master, self.unds, self.fo, self.db, self.tg, self.risk, self.tracker, hist, history_writer=self.history_writer)
        log.info("scanner initialized | history_symbols=%d | normal_trend_engine=%s | normal_live_intrabar=%s | momentum_orb=%s | momentum_explosion=%s",
                 len(hist), getattr(self.scanner, "normal_trend_diagnostics", None) is not None,
                 bool(getattr(self.scanner, "live_engine", None)),
                 bool(getattr(self.scanner, "momentum_orb", None)), bool(getattr(self.scanner, "mex", None)))
        # Exchange ban-list membership is informational only and never blocks signals.
        # Keep explicit operator-configured MANUAL_BAN exclusions as a separate override.
        manual_bans = {str(x).strip().upper() for x in cfg.MANUAL_BAN if str(x).strip()}
        self.scanner.ban = manual_bans
        self.ban_list_status = {
            "source": "disabled",
            "symbols": len(manual_bans),
            "manual_symbols": len(manual_bans),
            "paused_stock_symbols": 0,
            "stock_signals_paused": False,
            "date": today.isoformat(),
            "verified": False,
            "exchange_filter_enabled": False,
        }
        log.warning(
            "NSE F&O exchange ban-list filtering is DISABLED; no stock signals are paused. "
            "Verify current NSE ban status independently before acting | manual_exclusions=%d",
            len(manual_bans),
        )
        resumed = self._reload_active(master, today)
        if not self.started:
            self.ws.start()
            log.info("websocket start requested | enabled=%s", self.ws.enabled)
            self.tracker.start()
            self.started = True
        if self.ws.enabled:
            # Subscribe every resolved spot key, index future and India VIX key.
            keys = [cfg.UPSTOX_VIX_KEY]
            keys.extend(u.spot_key for u in self.unds if u.spot_key)
            keys.extend(u.fut_key for u in self.unds if u.kind == "INDEX" and u.fut_key)
            self.ws.subscribe(keys)
            ws_state = self.ws.status()
            missing_spot = [u.name for u in self.unds if not u.spot_key]
            log.info(
                "websocket subscriptions queued | expected_keys=%d | underlying_spots=%d | index_futures=%d | missing_spot_keys=%d | sent_on_current_connection=%d",
                len(set(keys)), sum(1 for u in self.unds if u.spot_key),
                sum(1 for u in self.unds if u.kind == "INDEX" and u.fut_key),
                len(missing_spot), ws_state.get("subscription_sent_count", 0),
            )
            if missing_spot:
                log.warning("websocket spot instrument keys missing | symbols=%s", ",".join(missing_spot))
        self.boot_day = today
        self.last_scan_boundary = None
        # The online_day key only selected notification wording; it is not a trading
        # dependency. A synchronous database read/write here could hold tick() in
        # daily_bootstrap indefinitely after the scanner and websocket were ready.
        startup_message = (
            f"✅ <b>F&O bot ready</b> {today}\nUniverse: {len(indices)} indices + "
            f"{len(normal_in_master)}/{len(normal_requested)} normal + "
            f"{len(mex_in_master)}/{len(mex_requested)} MEX (fixed lists)\n"
            f"Feed: UPSTOX ONLY | WS: {'on' if self.ws.enabled else 'off (REST polling)'} | DB: {self.db.backend}\n"
            f"Manual exclusions: {len(self.scanner.ban)} | Signals "
            f"{cfg.FIRST_SIGNAL_TIME[0]:02d}:{cfg.FIRST_SIGNAL_TIME[1]:02d}-"
            f"{cfg.LAST_SIGNAL_TIME[0]:02d}:{cfg.LAST_SIGNAL_TIME[1]:02d}, "
            f"exit {cfg.EXIT_TIME[0]:02d}:{cfg.EXIT_TIME[1]:02d}"
        )
        self._send_bootstrap_notices([
            "⚠️ NSE F&O ban-list auto-filtering is disabled. Signals may include stocks "
            "currently under exchange ban; verify the current NSE list before acting. "
            "Configured MANUAL_BAN exclusions remain active.",
            startup_message,
        ])
        log.info("daily bootstrap complete | date=%s | universe=%d | resumed_signals=%d", today, len(self.unds), resumed)

    @staticmethod
    def master_ref(m):
        return m

    def _reload_active(self, master: Master, today: date):
        """After a restart, resume tracking of today's open signals."""
        n = 0
        for s in self.db.active_signals(today.isoformat()):
            c = master.option(s.underlying, date.fromisoformat(s.expiry), s.strike, s.opt_type)
            if c and s.id not in self.tracker.active:
                self.tracker.add(s, c)
                n += 1
        return n

    # ---------- main loop ----------
    def run(self):
        """In-process driver (RUN_LOOP=true). /cron/tick calls the same idempotent tick(), so both can coexist."""
        log.info("Bot loop started")
        while not self._stop_event.is_set():
            try:
                r = self.tick()
                self._stop_event.wait(max(0.5, float(r.get("sleep", 5))))
            except Exception:
                log.exception("main loop error")
                self._stop_event.wait(10)

    def shutdown(self):
        """Gracefully stop tick producers before shutting down scanner executors."""
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
            self._stop_event.set()
            # Stop websocket callbacks first so no new futures can be submitted.
            try:
                self.ws.stop(timeout=5.0)
            except Exception:
                log.exception("websocket shutdown failed")
            # Let an already-running cron/loop tick finish before its scan
            # executors are closed; otherwise stage 1/2 can race pool shutdown.
            self._tick_lock.acquire()
            self._tick_lock.release()
            try:
                self.tracker.stop(timeout=3.0)
            except Exception:
                log.exception("tracker shutdown failed")
            try:
                self.history_writer.close(timeout=2.0)
            except Exception:
                log.exception("history candle writer shutdown failed")
            scanner = self.scanner
            if scanner is not None:
                try:
                    scanner.shutdown()
                except Exception:
                    log.exception("scanner executor shutdown failed")

    def tick(self, now=None, can_wait: bool = True) -> dict:
        """One idempotent step: tokens -> bootstrap (once/day) -> summary -> scan if a new 5-min candle has closed.
        Safe to call as often as you like, from the loop, from a cron webhook, or both."""
        if not self._tick_lock.acquire(blocking=False):
            return {"status": "busy", "sleep": 2}
        self._tick_stage = "tick_enter"
        self._tick_stage_started = time.monotonic()
        try:
            # Preserve injected timestamps in tests; in production refresh after
            # potentially slow bootstrap/network work so scans use wall-clock time.
            res = self._tick(now or mh.now(), can_wait, refresh_clock=(now is None))
        except Exception as e:
            log.exception("tick failed")
            res = {"status": "error", "error": str(e)[:200], "sleep": 10}
        finally:
            self._tick_lock.release()
        res["at"] = mh.now().isoformat(timespec="seconds")
        self.last_tick = res
        self._last_tick_result = dict(res)
        self._last_tick_finished_at = mh.now().isoformat(timespec="seconds")
        self._tick_stage = "idle"
        self._tick_stage_started = None
        return res

    def _tick(self, t, can_wait: bool, refresh_clock: bool = False) -> dict:
        today = t.date()
        self._tick_stage = "session_gate"
        if not mh.is_trading_day(today) or t.time() < dtime(8, 45) or t.time() > dtime(15, 50):
            return {"status": "idle", "sleep": 30}
        self._tick_stage = "token_refresh"
        tokens.ensure_fresh()
        if self.boot_day != today:
            self._tick_stage = "daily_bootstrap"
            if time.time() - self._boot_fail < 60:
                return {"status": "boot_backoff", "sleep": 15}
            try:
                self.bootstrap(today)
                # Bootstrap downloads instrument/history data and can take long
                # enough to miss the candle boundary that triggered this tick.
                # Never scan using the timestamp captured before that work.
                if refresh_clock:
                    t = mh.now()
                    today = t.date()
            except Exception as exc:
                self._boot_fail = time.time()
                self._alert_failure("bootstrap", "Daily bootstrap failed", exc)
                raise
        self._tick_stage = "post_bootstrap"
        if t.time() >= dtime(9, 21) and self.day_checked != today and self.unds:
            self._tick_stage = "holiday_check_scheduled"
            self._schedule_holiday_check(today)
        self._tick_stage = "scan_schedule"
        if self.closed_day == today:
            return {"status": "market_closed_today", "sleep": 60}
        if t.time() >= dtime(15, 35) and self.summary_day != today and self.db.kv_get("summary_day") != today.isoformat():
            self._summary(today)
            self.summary_day = today
            self.db.kv_set("summary_day", today.isoformat())

        step = cfg.SCAN_MINUTES
        b = t.replace(minute=t.minute - t.minute % step, second=0, microsecond=0)
        due = b + timedelta(seconds=cfg.SCAN_DELAY_SEC)
        if t < due:                                     # newest candle not final yet
            wait = (due - t).total_seconds()
            if not (can_wait and wait <= 25):
                return {"status": "waiting", "boundary": b.strftime("%H:%M"), "sleep": max(1.0, wait)}
            time.sleep(wait)
            t = mh.now()
        nxt = b + timedelta(minutes=step, seconds=cfg.SCAN_DELAY_SEC) - t
        # Sleeping up to the next boundary avoids waking every 20 seconds just to
        # repeat the same stale-boundary warning after a slow cold start.
        sleep = max(1.0, min(60.0, nxt.total_seconds()))
        # A delayed boot or blocked provider call can make the latest closed
        # candle older than the scanner's freshness limit. Skip this boundary;
        # never relax stale-data rules or generate a late signal.
        max_lag = max(0, int(cfg.MAX_CANDLE_LAG_SEC))
        if t > due + timedelta(seconds=max_lag):
            if self._last_missed_boundary != b:
                log.warning(
                    "Skipping missed scan boundary %s; current time %s is beyond the %ss candle freshness window; next boundary in %.1fs",
                    b.strftime("%H:%M:%S"), t.strftime("%H:%M:%S"), max_lag, max(0.0, nxt.total_seconds()),
                )
                self._last_missed_boundary = b
            return {"status": "missed_boundary", "boundary": b.strftime("%H:%M"), "sleep": sleep}
        if self.last_scan_boundary == b:
            return {"status": "waiting", "boundary": b.strftime("%H:%M"), "sleep": sleep}
        if not (mh.can_signal(t) and self.scanner):
            return {"status": "no_scan_window", "sleep": sleep}
        # Mark the candle only after run_once succeeds. If the scanner raises,
        # tick() can retry the same still-fresh candle instead of silently losing it.
        self._tick_stage = "scanner_run"
        sigs = self.scanner.run_once(t)
        self.last_scan_boundary = b
        self._last_missed_boundary = None
        self._digest(t, sigs)
        return {"status": "scanned", "boundary": b.strftime("%H:%M"), "signals": len(sigs), "stats": self.scanner.last_stats, "sleep": sleep}

    def _first_scan_report(self, t, sigs):
        """Once per day, after the first scan: confirm data is flowing (or say exactly what is not)."""
        today = t.date()
        if getattr(self, "_first_scan_report_day", None) == today:
            return
        self._first_scan_report_day = today
        try:
            st = (self.scanner.last_stats if self.scanner else None) or {}
            d = st.get("stage1_diagnostics") or {}
            hb = self.history_bootstrap_diagnostics or {}
            normal = st.get("normal_trend_engine") or {}
            gates = sorted((normal.get("gate_failures") or {}).items(), key=lambda kv: -kv[1])[:4]
            universe = int(d.get("universe", 0) or 0)
            usable = max(0, universe - int(d.get("banned_or_event", 0) or 0) - int(d.get("fetch_empty_or_failed", 0) or 0)
                         - int(d.get("stale_candles", 0) or 0) - int(d.get("no_closed_candles", 0) or 0))
            lines = [f"🔎 <b>{t:%H:%M} first scan of the day</b>",
                     f"Usable candles {usable}/{universe} | failed {d.get('fetch_empty_or_failed', 0)} | "
                     f"stale {d.get('stale_candles', 0)} | no closed candle {d.get('no_closed_candles', 0)} | "
                     f"rule errors {d.get('evaluation_errors', 0)}",
                     f"History: {hb.get('status', 'n/a')} | short {hb.get('short_history', 0)} symbols | "
                     f"rule scanned {normal.get('scanned', 0)} | triggers {normal.get('detected', 0)} | signals sent {len(sigs)}"]
            if gates:
                lines.append("Rule blocked by: " + ", ".join(f"{g} ×{c}" for g, c in gates))
            if usable < max(1, universe // 2):
                lines.append("⚠️ Most symbols have no usable candles - check the Upstox token and /diagnostics.")
            self.tg.send("\n".join(lines))
        except Exception:
            log.exception("first scan report failed")

    def _digest(self, t, sigs):
        """Every SCAN_DIGEST_EVERY scans without a signal, tell Telegram *why* (so silence is never a mystery)."""
        self._first_scan_report(t, sigs)
        self._data_health_alert(t)
        n = cfg.SCAN_DIGEST_EVERY
        if n <= 0:
            return
        self._quiet_scans = 0 if sigs else getattr(self, "_quiet_scans", 0) + 1
        if sigs or self._quiet_scans % n:
            return
        st = self.scanner.last_stats or {}
        d = st.get("stage1_diagnostics", {})
        why = sorted((st.get("no_setup_reasons") or {}).items(), key=lambda kv: -kv[1])[:4]
        rej = sorted((st.get("rejects") or {}).items(), key=lambda kv: -kv[1])[:4]
        lines = [f"🕒 <b>{t:%H:%M} scan: no signal for {self._quiet_scans} scans</b>",
                 f"Universe {d.get('universe', 0)} | data failed {d.get('fetch_empty_or_failed', 0)} | stale {d.get('stale_candles', 0)} | "
                 f"price setups {st.get('stage1_hits', 0)} | NIFTY 15m bias {st.get('mkt_bias', 0):+d}"]
        if why:
            lines.append("No setup because: " + ", ".join(f"{k} ×{v}" for k, v in why))
        if rej:
            lines.append("Rejected later: " + ", ".join(f"{k} ×{v}" for k, v in rej))
        normal = st.get("normal_trend_engine") or {}
        normal_gates = sorted((normal.get("gate_failures") or {}).items(),
                              key=lambda kv: -kv[1])[:4]
        engine_label = "Simple trend" if normal.get("engine") == "SIMPLE" else "Normal Trend"
        if normal:
            lines.append(
                f"{engine_label}: "
                f"scanned {normal.get('scanned', 0)} | detected {normal.get('detected', 0)} | "
                f"selected {normal.get('selected', 0)} | superseded {normal.get('priority_superseded', 0)}"
            )
        if normal_gates:
            lines.append(f"{engine_label} blocked by: " + ", ".join(f"{gate} ×{count}" for gate, count in normal_gates))
        self.tg.send("\n".join(lines))

    def _alert_failure(self, kind: str, title: str, exc: Exception, cooldown: float = 1800.0):
        """Telegram notice for a failure that otherwise only reaches the logs; throttled per kind."""
        try:
            alerts = self.__dict__.setdefault("_failure_alerts", {})
            now_mono = time.monotonic()
            if kind in alerts and now_mono - alerts[kind] < cooldown:
                return
            alerts[kind] = now_mono
            self.tg.send(f"🛑 <b>{html.escape(title)}</b>\n{html.escape(type(exc).__name__)}: "
                         f"{html.escape(str(exc)[:300])}\n"
                         "The scanner cannot run until this succeeds; retrying automatically.")
        except Exception:
            log.exception("failure alert could not be sent")

    def _data_health_alert(self, t):
        """Warn on Telegram when most of the universe had no usable candles in the latest scan.

        A scanner that cannot read data looks exactly like a market with no setups; this makes the difference visible.
        Throttled by DATA_HEALTH_ALERT_COOLDOWN_SEC so a long outage does not spam the chat.
        """
        try:
            st = (self.scanner.last_stats if self.scanner else None) or {}
            d = st.get("stage1_diagnostics") or {}
            universe = int(d.get("universe", 0) or 0)
            if universe <= 0:
                return
            bad = sum(int(d.get(k, 0) or 0) for k in
                      ("fetch_empty_or_failed", "stale_candles", "no_closed_candles"))
            if bad / universe < float(cfg.DATA_HEALTH_ALERT_FRAC):
                return
            now_mono = time.monotonic()
            last = getattr(self, "_last_health_alert", None)
            if last is not None and now_mono - last < float(cfg.DATA_HEALTH_ALERT_COOLDOWN_SEC):
                return
            self._last_health_alert = now_mono
            last_error = getattr(self.fo, "last_error", None)
            self.tg.send(
                f"⚠️ <b>{t:%H:%M} DATA PROBLEM: scanner cannot read candles</b>\n"
                f"{bad}/{universe} symbols unusable | failed/empty {d.get('fetch_empty_or_failed', 0)} "
                f"(timed out {d.get('fetch_timed_out', 0)}) | stale {d.get('stale_candles', 0)} | "
                f"no closed candle {d.get('no_closed_candles', 0)}\n"
                f"Last provider error: {last_error or 'none recorded'}\n"
                "No signals are possible until data recovers. Check the Upstox token and /diagnostics.")
        except Exception:
            log.exception("data health alert failed")

    def _schedule_holiday_check(self, today: date):
        """Run the optional first-session probe off the scanner/tick control thread."""
        if self.day_checked == today:
            return
        with self._holiday_check_lock:
            thread = self._holiday_check_thread
            if thread is not None and thread.is_alive():
                return
            started_at = mh.now().isoformat(timespec="seconds")
            self._holiday_check_status = {
                "state": "running", "date": today.isoformat(),
                "started_at": started_at, "finished_at": None, "error": None,
            }

            def _check():
                error = None
                try:
                    self._holiday_check(today)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:240]
                    log.exception("background market-session check failed")
                with self._holiday_check_lock:
                    state = "confirmed" if self.day_checked == today else ("failed" if error else "unconfirmed")
                    self._holiday_check_status = {
                        "state": state, "date": today.isoformat(),
                        "started_at": started_at,
                        "finished_at": mh.now().isoformat(timespec="seconds"), "error": error,
                    }

            self._holiday_check_thread = threading.Thread(
                target=_check, name="market-session-check", daemon=True
            )
            self._holiday_check_thread.start()

    def _holiday_check(self, today: date):
        """No NIFTY candles after 9:21 on a weekday = exchange holiday not listed in holidays.json."""
        u = self.unds_by_name.get("NIFTY") or (self.unds[0] if self.unds else None)
        if not u:
            return
        try:
            raw = self.fo.call("spot_intraday", u)
        except Exception:
            return                                   # try again next tick
        # An empty intraday response is not proof of an exchange holiday. It can
        # also mean a delayed/temporarily unavailable feed. Never latch closed_day
        # from one empty response: doing so suppresses every later scan that day.
        if not any(c.ts.date() == today for c in raw):
            wall = time.time()
            if wall - self._holiday_empty_warn_at >= 300:
                log.warning(
                    "market-session check returned no %s candles for %s; not declaring a holiday; will retry",
                    u.name, today,
                )
                self._holiday_empty_warn_at = wall
            return
        self.day_checked = today
        log.info("market-session check confirmed current-day %s candles | date=%s | bars=%d",
                 u.name, today, sum(1 for c in raw if c.ts.date() == today))

    def _summary(self, today: date):
        sigs = self.db.signals_on(today.isoformat())
        closed = [s for s in sigs if s.status == "CLOSED"]
        wins = [s for s in closed if s.pnl_lot > 0]
        losses = [s for s in closed if s.pnl_lot < 0]
        pnl = sum(s.pnl_lot for s in closed)
        self.db.save_daily(today.isoformat(), len(sigs), len(wins), len(losses), pnl)
        lines = [f"📊 <b>Daily summary {today}</b>", f"Signals: {len(sigs)} | Wins {len(wins)} | Losses {len(losses)} | Open {len(sigs) - len(closed)}",
                 f"Paper P&L per 1 lot each: ₹{pnl:,.0f}"]
        for s in sigs:
            lines.append(f"• {s.underlying} {s.side} {s.strike:g}{s.opt_type}: {s.result or s.status} ₹{s.pnl_lot:,.0f}")
        try:
            lines.append("")
            lines.append(format_report(build_report(self.db, 30, today), compact=True))
        except Exception:
            log.exception("report failed")
        self.tg.send("\n".join(lines))

    def report(self, days: int = 30) -> dict:
        rep = build_report(self.db, max(1, min(365, days)), mh.now().date())
        adaptive = self.scanner.adaptive.snapshot(mh.now()) if self.scanner else {}
        return {"report": rep, "adaptive": adaptive, "text": format_report(rep, adaptive)}

    def diagnostics(self) -> dict:
        """Expanded operational diagnostics; callers must protect this behind CRON_SECRET."""
        simple_mode = str(cfg.STRATEGY_MODE).upper() != "LEGACY"
        if simple_mode:
            normal_indicators = ["EMA9/21", "session_VWAP", "RSI(14)", "ADX(14)"]
            normal_setup_paths = ["SIMPLE_TREND"]
            normal_thresholds = {
                "adx_min": cfg.SIMPLE_ADX_MIN,
                "rsi_call_min": cfg.SIMPLE_RSI_CALL_MIN,
                "rsi_call_max": cfg.SIMPLE_RSI_CALL_MAX,
                "rsi_put_min": cfg.SIMPLE_RSI_PUT_MIN,
                "rsi_put_max": cfg.SIMPLE_RSI_PUT_MAX,
                "fresh_bars": cfg.SIMPLE_FRESH_BARS,
                "max_entry_extension_atr": cfg.SIMPLE_MAX_EXTENSION_ATR,
                "minimum_first_target_rr": cfg.SIMPLE_MIN_RR_T1,
            }
            normal_15m_context = "reject_opposing_allow_neutral"
        else:
            normal_indicators = ["EMA9/21", "MACD(12/26/9)", "session_VWAP", "RSI(14)", "ADX(14)", "+DI/-DI"]
            normal_setup_paths = ["EARLY_TREND", "TREND_PULLBACK", "MOMENTUM_BREAKOUT"]
            normal_thresholds = {
                "adx_min": cfg.NORMAL_ADX_MIN,
                "adx_established": cfg.NORMAL_ADX_ESTABLISHED,
                "di_min_gap": cfg.NORMAL_DI_MIN_GAP,
                "rsi_long_min": cfg.NORMAL_RSI_LONG_MIN,
                "rsi_short_max": cfg.NORMAL_RSI_SHORT_MAX,
                "candle_body_min": cfg.NORMAL_CANDLE_BODY_MIN,
                "close_position_min": cfg.NORMAL_CLOSE_POS_MIN,
                "pullback_atr": cfg.NORMAL_PULLBACK_ATR,
                "max_entry_extension_atr": cfg.NORMAL_MAX_EXTENSION_ATR,
                "breakout_lookback": cfg.NORMAL_BREAKOUT_LOOKBACK,
            }
            normal_15m_context = "informational_only"
        modules = {
            "normal_strategy_mode": "SIMPLE" if simple_mode else ("V3" if cfg.V3_PROFILE_ENABLED else "LEGACY"),
            "v3_profile_enabled": bool(cfg.V3_PROFILE_ENABLED),
            "normal_trend_engine": True,
            "normal_indicators": normal_indicators,
            "normal_setup_paths": normal_setup_paths,
            "normal_thresholds": normal_thresholds,
            "normal_diagnostics": ["scanned", "detected", "selected", "priority_superseded",
                                   "no_trigger", "setups", "gate_failures", "per_symbol_latest"],
            "normal_15m_context": normal_15m_context,
            "normal_fixed_stock_universe": len(NORMAL_SYMBOLS),
            "orb_engine": cfg.MOMENTUM_ORB_ENABLED,
            "orb_fixed_universe": 100,
            "orb_opening_range": "09:15-09:30",
            "orb_last_entry_time": f"{cfg.ORB_LAST_TIME[0]:02d}:{cfg.ORB_LAST_TIME[1]:02d}",
            "orb_retest_required": False,
            "orb_diagnostics": "eligible/candidates/confirmed/expired/invalidated/no_trigger/rejection_reasons",
            "mex_diagnostics": "eligible/detected/confirmed/no_trigger/rejection_reasons/gate_failures",
            "mex_engine": cfg.MEX_ENABLED,
            "mex_fixed_universe": 100,
            "mex_next_candle_confirmation_required": False,
            "live_intrabar_pipeline": bool(cfg.LIVE_INTRABAR_ENABLED),
            "live_intrabar_trigger_mode": "upstox_websocket_partial_bar" if cfg.LIVE_INTRABAR_ENABLED else "disabled",
            "legacy_v7_sequential_prerequisite": False,
            "signal_only": True,
            "orders_enabled": False,
            "option_chain_for_contract_selection": True,
        }
        last_scan = getattr(self.scanner, "last_stats", {}) if self.scanner else {}
        ws_status = self.ws.status()
        ws_expected_keys = set(ws_status.get("expected_subscription_keys", []))
        ws_sent_keys = set(ws_status.get("subscription_sent_keys", []))
        ws_tick_ages = ws_status.get("per_key_tick_age_seconds") or {}
        return {
            "ok": True, "time": mh.now().isoformat(timespec="seconds"),
            "database": {"backend": self.db.backend, "persistent_required": True,
                         "postgres_connected": self.db.backend == "postgres"},
            "providers": self.fo.health(), "history_bootstrap": self.history_bootstrap_diagnostics,
            "universe": {"count": len(self.unds), "symbols": [u.name for u in self.unds],
                **self.universe_diagnostics},
            "setup_modules": modules,
            "v7_config": {"adx_min": cfg.V7_ADX_MIN, "min_bars": cfg.V7_MIN_BARS,
                          "history_days": cfg.HIST_DAYS, "cross_max_bars": cfg.V7_CROSSOVER_MAX_BARS,
                          "vwap_max_atr": cfg.V7_VWAP_MAX_ATR,
                          "pullback_trend_bars": cfg.V7_PULLBACK_TREND_BARS,
                          "pullback_touch_atr": cfg.V7_PULLBACK_TOUCH_ATR},
            "last_scan": last_scan,
            "activity": self.db.activity_summary(mh.now().date().isoformat()),
            "telegram_outbox_worker": dict(getattr(self.scanner, "_outbox_status", {}) or {}) if self.scanner else {},
            "candidate_watchlist_worker": dict(getattr(self.scanner, "_candidate_persist_status", {}) or {}) if self.scanner else {},
            "candle_archive_writer": self.history_writer.snapshot(),
            "normal_live_intrabar": (
                self.scanner.live_engine.normal_snapshot()
                if self.scanner and getattr(self.scanner, "live_engine", None) else {}
            ),
            "scan_lifecycle": {
                "started_at": getattr(self.scanner, "last_cycle_started_at", None) if self.scanner else None,
                "completed_at": getattr(self.scanner, "last_cycle_at", None) if self.scanner else None,
                "stage": getattr(self.scanner, "last_cycle_stage", "not_bootstrapped") if self.scanner else "not_bootstrapped",
                "running": bool(self.scanner and getattr(self.scanner, "last_cycle_stage", "idle") not in {"idle", "completed", "failed"}),
                "stage_elapsed_seconds": (
                    round(max(0.0, time.monotonic() - self.scanner._cycle_stage_started), 1)
                    if self.scanner and getattr(self.scanner, "_cycle_stage_started", None) is not None
                    and getattr(self.scanner, "last_cycle_stage", "idle") not in {"idle", "completed", "failed"}
                    else 0.0
                ),
                "error": getattr(self.scanner, "last_cycle_error", None) if self.scanner else None,
                "duration_seconds": getattr(self.scanner, "last_cycle_seconds", None) if self.scanner else None,
                "stage_seconds": getattr(self.scanner, "last_cycle_stage_seconds", {}) if self.scanner else {},
                "prefetch": getattr(self.scanner, "prefetch_diagnostics", {}) if self.scanner else {},
            },
            "market_session": {
                "day_checked": self.day_checked.isoformat() if self.day_checked else None,
                "holiday_check": dict(self._holiday_check_status),
                "closed_day": self.closed_day.isoformat() if self.closed_day else None,
                "holiday_empty_response_last_warning_age_sec": (
                    round(max(0.0, time.time() - self._holiday_empty_warn_at), 1)
                    if self._holiday_empty_warn_at else None
                ),
                "is_trading_day": mh.is_trading_day(mh.now().date()),
                "can_signal_now": mh.can_signal(mh.now()),
            },
            "ban_list": getattr(self, "ban_list_status", {"source": "not_loaded", "verified": False}),
            "websocket": {**ws_status,
                          "subscribed_underlyings": len(self.unds_by_name),
                          "symbols_missing_spot_key": [u.name for u in self.unds if not u.spot_key],
                          "symbol_tick_status": {
                              u.name: {
                                  "spot_key": u.spot_key,
                                  "expected": bool(u.spot_key and u.spot_key in ws_expected_keys),
                                  "subscription_sent": bool(u.spot_key and u.spot_key in ws_sent_keys),
                                  "tick_age_seconds": ws_tick_ages.get(u.spot_key) if u.spot_key else None,
                              }
                              for u in self.unds
                          },
                          "live_intrabar_engine": bool(self.scanner and self.scanner.live_engine)},
            "bootstrap_notifications": dict(self._bootstrap_notice_status),
            "tokens": tokens.status(),
            "telegram": {"configured": bool(self.tg.token and self.tg.chat_ids),
                         "chat_count": len(self.tg.chat_ids),
                         "last_ok_ts": getattr(self.tg, "last_ok_ts", None),
                         "last_error": getattr(self.tg, "last_error", None),
                         "outbox": self.db.telegram_outbox_summary()},
        }

    def health_probe(self) -> dict:
        """Cheap in-memory health state; must never call providers or the database."""
        scanner = self.scanner
        stage_started = self._tick_stage_started
        return {
            "ok": True,
            "time": mh.now().isoformat(timespec="seconds"),
            "booted_for": self.boot_day.isoformat() if self.boot_day else None,
            "tick": {
                "stage": self._tick_stage,
                "stage_elapsed_seconds": round(max(0.0, time.monotonic() - stage_started), 1) if stage_started else 0.0,
                "last_result": self._last_tick_result,
                "last_finished_at": self._last_tick_finished_at,
            },
            "websocket": self.ws.status(),
            "bootstrap_notifications": dict(self._bootstrap_notice_status),
            "universe_count": len(self.unds),
            "market_session_check": dict(self._holiday_check_status),
            "telegram_outbox_worker": dict(getattr(scanner, "_outbox_status", {}) or {}) if scanner else {},
            "candidate_watchlist_worker": dict(getattr(scanner, "_candidate_persist_status", {}) or {}) if scanner else {},
            "candle_archive_writer": self.history_writer.snapshot(),
            "scanner": {
                "available": scanner is not None,
                "stage": getattr(scanner, "last_cycle_stage", "not_bootstrapped") if scanner else "not_bootstrapped",
                "running": bool(scanner and getattr(scanner, "last_cycle_stage", "idle") not in {"idle", "completed", "failed"}),
                "last_cycle_started_at": getattr(scanner, "last_cycle_started_at", None) if scanner else None,
                "last_cycle_at": getattr(scanner, "last_cycle_at", None) if scanner else None,
                "last_cycle_error": getattr(scanner, "last_cycle_error", None) if scanner else None,
            },
        }

    def health(self) -> dict:
        return {"ok": True, "time": mh.now().isoformat(timespec="seconds"), "provider": self.fo.health(),
                "ws": self.ws.status(), "universe": len(self.unds), "active_signals": len(self.tracker.signals()),
                "booted_for": self.boot_day.isoformat() if self.boot_day else None, "db": self.db.backend,
                "mode": "loop+cron" if cfg.RUN_LOOP else "cron-only", "last_tick": self.last_tick,
                "last_scan": getattr(self.scanner, "last_stats", {}) if self.scanner else {}, "tokens": tokens.status(),
                "scanner_last_cycle_at": getattr(self.scanner, "last_cycle_at", None) if self.scanner else None,
                "scanner_last_cycle_started_at": getattr(self.scanner, "last_cycle_started_at", None) if self.scanner else None,
                "scanner_last_cycle_stage": getattr(self.scanner, "last_cycle_stage", "not_bootstrapped") if self.scanner else "not_bootstrapped",
                "scanner_cycle_running": bool(self.scanner and getattr(self.scanner, "last_cycle_stage", "idle") not in {"idle", "completed", "failed"}),
                "scanner_current_stage_elapsed_seconds": (
                    round(max(0.0, time.monotonic() - self.scanner._cycle_stage_started), 1)
                    if self.scanner and getattr(self.scanner, "_cycle_stage_started", None) is not None
                    and getattr(self.scanner, "last_cycle_stage", "idle") not in {"idle", "completed", "failed"}
                    else 0.0
                ),
                "scanner_last_cycle_error": getattr(self.scanner, "last_cycle_error", None) if self.scanner else None,
                "scanner_last_cycle_stage_seconds": getattr(self.scanner, "last_cycle_stage_seconds", {}) if self.scanner else {},
                "telegram_last_ok_ts": getattr(self.tg, "last_ok_ts", None) if getattr(self, "tg", None) else None,
                "telegram_last_error": getattr(self.tg, "last_error", None) if getattr(self, "tg", None) else None,
                "telegram_outbox": self.db.telegram_outbox_summary(),
                "scanner_last_cycle_seconds": getattr(self.scanner, "last_cycle_seconds", None) if self.scanner else None,
                "provider_latency_ms": self.fo.latency_ms,
                "strategy_performance": strategy_performance(self.db.signals_on(mh.now().date().isoformat()))}
