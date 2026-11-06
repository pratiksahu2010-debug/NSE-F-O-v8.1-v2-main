"""Three-stage funnel: (1) cheap spot scan -> (2) OI/chain enrichment -> (3) score, pick contract, risk, alert."""
import hashlib
import logging
import math
import threading
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

from config import cfg
from data import events
from data.instruments import Master
from indicators.core import atr, htf_bias, htf_vwap_bias, iv_rank as calc_iv_rank, with_volume
from models import Candle, Signal, Underlying, Vote
from strategies.price import (all_setups, invalidation_distance, nearest_structure_obstacle,
                              price_diagnose, price_score, price_votes, strategy_of)
from strategies.regime import regime as calc_regime
from engine import context as ctx, scorer
from engine.chain_quality import chain_quality_issues
from engine.entry_validation import EntryCandidate, validate_entry_candidate
from engine.live_intrabar import LiveIntrabarEngine
from strategies.mex_scanner import MomentumExplosionScanner
from strategies.momentum_orb import MomentumORBScanner
from strategies.normal_universe import NORMAL_SYMBOL_SET
from strategies.simple_signal import evaluate_simple
from v7.sequential import SequentialEngine
from engine.adaptive import Adaptive
from engine.contract_picker import pick_contract, pick_expiry
from telegram_service import format_signal
from utils.market_hours import can_signal, IST
from signal_engine_v2.runtime import create_engine as create_v2_engine, evaluate_legacy_candles
from signal_engine_v2.models import DecisionState as V2DecisionState
from signal_engine_v2.config import EngineConfig as V2EngineConfig
from signal_engine_v2.contracts import (validate_option_quote, select_validated_contract)

log = logging.getLogger("scanner")


def _executable_buy_price(quote) -> Optional[float]:
    """A long option entry must use a live ask; LTP is not an executable buy quote."""
    try:
        ask = float(getattr(quote, "ask", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return ask if ask > 0 else None


def _closed(candles: List[Candle], now: datetime) -> List[Candle]:
    """Return valid, closed, chronologically ordered candles with duplicate bars removed.

    Providers can return overlapping history windows or partial/malformed OHLC
    rows. Letting those through distorts ATR/ADX/EMA and can create phantom
    breakouts. For duplicate timestamps, the last valid row wins.
    """
    by_ts = {}
    # Providers occasionally return naive datetimes. Interpret those as IST and
    # normalize both sides before comparisons to avoid aware/naive conflicts.
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    for c in candles:
        try:
            cts = c.ts
            if cts.tzinfo is None:
                cts = cts.replace(tzinfo=IST)
            else:
                cts = cts.astimezone(IST)
            if cts + timedelta(minutes=cfg.SCAN_MINUTES) > now:
                continue
            vals = (c.o, c.h, c.l, c.c)
            if not all(float(v) > 0 for v in vals):
                continue
            if c.h < max(c.o, c.c, c.l) or c.l > min(c.o, c.c, c.h) or c.h < c.l:
                continue
            if c.v < 0 or c.oi < 0:
                continue
            if cts != c.ts:
                c = Candle(ts=cts, o=c.o, h=c.h, l=c.l, c=c.c, v=c.v, oi=c.oi)
            by_ts[cts] = c
        except (TypeError, ValueError, OverflowError, AttributeError):
            continue
    return [by_ts[ts] for ts in sorted(by_ts)]


def _stage1_fetch_results(pool, symbols, fetcher, timeout_seconds):
    """Yield Stage-1 fetches in completion order with a single bounded wall-clock budget.

    Running requests cannot be forcibly interrupted by ThreadPoolExecutor, but
    their results never hold up processing of already completed symbols.
    """
    started = time.monotonic()
    jobs = {pool.submit(fetcher, symbol): symbol for symbol in symbols}
    completed = 0
    try:
        for future in as_completed(jobs, timeout=max(0.01, float(timeout_seconds))):
            symbol = jobs[future]
            elapsed = time.monotonic() - started
            try:
                result = future.result()
                error = None
            except Exception as exc:
                result = (None, None)
                error = f"{type(exc).__name__}: {exc}"
                log.warning("stage1 spot request failed | symbol=%s | elapsed=%.1fs | error=%s",
                            symbol.name, elapsed, error)
            completed += 1
            log.info("stage1 symbol fetch completed | symbol=%s | progress=%d/%d | elapsed=%.1fs | status=%s",
                     symbol.name, completed, len(jobs), elapsed, "ok" if error is None else "error")
            yield symbol, result, error
    except FutureTimeoutError:
        pass

    for future, symbol in jobs.items():
        if future.done():
            continue
        future.cancel()
        elapsed = time.monotonic() - started
        log.warning("stage1 spot request timed out | symbol=%s | elapsed=%.1fs | budget=%.1fs",
                    symbol.name, elapsed, timeout_seconds)
        yield symbol, (None, None), "timeout"


def _normal_15m_context_matches(side: str, htf: int, allow_neutral: bool = True) -> bool:
    """Check completed 15m direction; V3 can require a directional (non-neutral) context."""
    expected = 1 if str(side).upper() == "CALL" else -1 if str(side).upper() == "PUT" else 0
    actual = int(htf or 0)
    return expected != 0 and (actual == expected or (allow_neutral and actual == 0))


def fresh_reference(earlier: Optional[dict], now: datetime) -> Optional[dict]:
    """An OI snapshot is only a valid "~30 minutes ago" reference if it is from today and not older than
    OI_REF_MAX_AGE_MIN. Snapshots are only written for symbols that reached stage 2, so the latest one can easily be
    hours (or a day) old, which used to create phantom PCR-shift / wall-break votes."""
    if not earlier:
        return None
    try:
        ts = datetime.fromisoformat(earlier["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        else:
            ts = ts.astimezone(IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        else:
            now = now.astimezone(IST)
        if ts.date() != now.date() or (now - ts) > timedelta(minutes=cfg.OI_REF_MAX_AGE_MIN):
            return None
    except Exception:
        return None
    return earlier


class Scanner:
    def __init__(self, master: Master, unds: List[Underlying], fo, db, tg, risk, tracker, hist: Dict[str, List[Candle]], history_writer=None):
        self.master, self.unds, self.fo, self.db, self.tg = master, unds, fo, db, tg
        self.risk, self.tracker, self.hist = risk, tracker, hist
        self.history_writer = history_writer
        self.adaptive = Adaptive(db)
        self.v2_engine = create_v2_engine(config=V2EngineConfig(
            min_score=float(cfg.V2_MIN_SCORE),
            min_risk_reward=float(cfg.V2_MIN_RR),
            min_history_bars=int(cfg.V2_MIN_HISTORY_BARS),
            min_adx=22.0,
        ))
        self._v2_preview_lock = threading.Lock()
        self._v2_early_previews = {}
        self.ban: Set[str] = set()
        self.last_stats = {}
        self.last_cycle_at: Optional[str] = None
        self.last_cycle_started_at: Optional[str] = None
        self.last_cycle_stage = "idle"
        self.last_cycle_error: Optional[str] = None
        self.last_cycle_stage_seconds: Dict[str, float] = {}
        self._cycle_stage_started = None
        self.mkt_bias = 0                      # NIFTY 15m trend (+1/-1/0), refreshed every scan in stage 1
        self.mkt_ret: Optional[float] = None   # NIFTY session return % (relative-strength reference)
        self._archived: Dict[str, str] = {}    # symbol -> ts of the last candle archived to the DB
        self._rej: Dict[str, int] = {}
        self._why: Dict[str, int] = {}        # stage-1 "why no price setup" histogram of the last scan
        self._symbol_rejections: Dict[str, dict] = {}  # per-symbol diagnostics for /diagnostics
        # Persist candidate lifecycle in the configured DB (Postgres on Render recommended); memory-only state was
        # lost on every restart, obscuring why candidates had progressed or expired.
        self.candidate_watchlist: Dict[str, dict] = {}
        self._candidate_lock = threading.RLock()
        try:
            saved = self.db.kv_get("candidate_watchlist_v1")
            if saved:
                parsed = __import__("json").loads(saved)
                if isinstance(parsed, dict):
                    self.candidate_watchlist = {str(k): v for k, v in parsed.items() if isinstance(v, dict)}
        except Exception:
            log.exception("candidate watchlist restore failed")
        self._closed = False
        self.pool = ThreadPoolExecutor(max_workers=max(1, cfg.SCAN_WORKERS), thread_name_prefix="scan")
        self.live_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="live-trigger")
        # Live trigger evaluation must never wait on option-chain REST latency.
        # One in-flight refresh per underlying/expiry prevents request storms.
        self._live_chain_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-chain")
        self._live_chain_lock = threading.Lock()
        self._live_chain_cache = {}
        self._live_chain_inflight = {}
        self._live_chain_retry_after = {}
        self.live_engine = LiveIntrabarEngine(cfg.SCAN_MINUTES, cfg.WS_STALE_SEC)
        self.live_context = {}
        self.underlying_by_key = {u.spot_key: u for u in self.unds if u.spot_key}
        self.underlying_by_fut_key = {u.fut_key: u for u in self.unds if u.kind == "INDEX" and u.fut_key}
        self._fut_volume_snapshot = {}
        self._live_inflight = set()
        self._live_state_lock = threading.Lock()
        self._live_eval_at = {}
        self._live_pending = set()  # at most one queued/running detector task per underlying
        self._live_symbol_locks = {u.name: threading.RLock() for u in self.unds}
        self.mex = MomentumExplosionScanner(db)   # independent MEX engine; shares only closed candle inputs
        self.momentum_orb = MomentumORBScanner(db)  # independent MEX+ORB indicator and retest lifecycle
        self.sequential = SequentialEngine(crossover_max_bars=cfg.V7_CROSSOVER_MAX_BARS, adx_min=cfg.V7_ADX_MIN,
                                          vwap_max_atr=cfg.V7_VWAP_MAX_ATR,
                                          rsi_call_max=cfg.V7_RSI_CALL_MAX, rsi_put_min=cfg.V7_RSI_PUT_MIN,
                                          min_history_bars=cfg.V7_MIN_BARS,
                                          pullback_trend_bars=cfg.V7_PULLBACK_TREND_BARS,
                                          pullback_touch_atr=cfg.V7_PULLBACK_TOUCH_ATR,
                                          max_ema_crosses=cfg.EMA_MAX_CROSSES, min_ema_sep_atr=cfg.EMA_MIN_SEP_ATR,
                                          max_vwap_crosses=cfg.VWAP_MAX_CROSSES)  # regime-aware confirmed sequential continuation
        self.v7_diagnostics = {}
        self.normal_trend_diagnostics = {}
        self.last_cycle_seconds: Optional[float] = None
        self._outbox_lock = threading.Lock()
        self._outbox_thread = None
        self._outbox_db = None
        self._outbox_status = {"state": "idle", "started_at": None, "finished_at": None, "error": None}
        self._maintenance_db_lock = threading.Lock()
        self._candidate_persist_lock = threading.Lock()
        self._candidate_persist_thread = None
        self._candidate_persist_dirty = False
        self._candidate_persist_status = {"state": "idle", "started_at": None, "finished_at": None, "error": None}
        # V8.1: closed-candle readiness (shared by all stage-1 workers) and per-day history backfill bookkeeping.
        self._scan_now: Optional[datetime] = None
        self._ready_lock = threading.Lock()
        self._ready_state = {"ready": False, "gave_up": False}
        self._backfill_lock = threading.Lock()
        self._backfill_day = None
        self._backfill_attempts: Dict[str, int] = {}
        self._backfill_this_scan = 0

    def _set_cycle_stage(self, stage: str):
        """Record bounded stage timing so a stalled/failed scan is diagnosable."""
        current = time.monotonic()
        previous = getattr(self, "last_cycle_stage", "idle")
        started = getattr(self, "_cycle_stage_started", None)
        if started is not None and previous not in {"idle", "completed"}:
            self.last_cycle_stage_seconds[previous] = round(max(0.0, current - started), 3)
        self.last_cycle_stage = str(stage)
        self._cycle_stage_started = current
        if stage not in {"completed", "failed"}:
            log.info("scan stage started | stage=%s", stage)

    def _watch(self, symbol: str, side: str, candle_ts, price_score_100: float, status: str, score_10=None, reasons=None, now=None):
        """Keep candidate lifecycle separate from actionable signal history."""
        key = f"{symbol}:{side}"
        new_candle = candle_ts.isoformat() if hasattr(candle_ts, "isoformat") else str(candle_ts)
        with self._candidate_lock:
            previous = self.candidate_watchlist.get(key, {})
            if previous.get("candle_time") == new_candle and previous.get("status") in {"SIGNAL_SENT", "INVALIDATED"}:
                return
            self.candidate_watchlist[key] = {
                "symbol": symbol, "direction": side, "candle_time": new_candle,
                "price_score_100": round(abs(float(price_score_100)) * 100, 1),
                "alert_score_10": round(float(score_10), 2) if score_10 is not None else None,
                "status": status, "reasons": list(reasons or []),
                "updated_at": now.isoformat(timespec="seconds") if hasattr(now, "isoformat") else None,
            }
            # Bound storage to recent candidates; terminal states are retained for diagnostics/deduplication.
            if len(self.candidate_watchlist) > 500:
                ordered = sorted(self.candidate_watchlist.items(),
                                 key=lambda kv: str(kv[1].get("updated_at", "")))
                for old_key, _ in ordered[:len(self.candidate_watchlist) - 500]:
                    self.candidate_watchlist.pop(old_key, None)
        # Production uses asynchronous persistence; injected test databases
        # retain their synchronous durability contract for deterministic fixtures.
        if getattr(self.db, "backend", None) == "postgres":
            self._schedule_candidate_watchlist_persistence()
        else:
            try:
                self.db.kv_set("candidate_watchlist_v1", __import__("json").dumps(self.candidate_watchlist))
            except Exception:
                log.exception("candidate watchlist persistence failed")

    def _note_symbol(self, name: str, stage: str, reason: str, detail: str = ""):
        if not name:
            return
        item = self._symbol_rejections.setdefault(name, {"stage": stage, "reasons": []})
        item["stage"] = stage
        entry = reason if not detail else f"{reason}: {detail}"
        if entry not in item["reasons"]:
            item["reasons"].append(entry)
        # Keep the diagnostics response compact and bounded.
        item["reasons"] = item["reasons"][-8:]

    def _archive(self, name: str, session: List[Candle]):
        """Persist closed session candles so tools/replay.py can test changes on real, timestamp-safe data."""
        if not cfg.CANDLE_ARCHIVE_ENABLED or not session:
            return
        last = self._archived.get(name)
        new = [c for c in session if last is None or c.ts.isoformat() > last]
        if not new:
            return
        if self.history_writer is not None:
            try:
                self.history_writer.submit(name, new)
                # Mark submitted, not remotely committed: the isolated writer
                # owns retries/checkpoint recovery on the next bootstrap.
                self._archived[name] = new[-1].ts.isoformat()
            except Exception:
                log.exception("asynchronous candle archive queue failed for %s", name)
            return
        try:
            self.db.save_candles(name, new)
            self._archived[name] = new[-1].ts.isoformat()
        except Exception:
            log.exception("candle archive failed for %s", name)

    def _reject(self, why: str, name: str = "", detail: str = ""):
        """Count and retain a rejected candidate in diagnostics and durable daily history."""
        key = why.split(":")[0].split(" (")[0][:80]
        self._rej[key] = self._rej.get(key, 0) + 1
        self._note_symbol(name, "stage2/3", why, detail)
        if name:
            try:
                recorder = getattr(self.db, "record_rejection", None)
                if callable(recorder):
                    now = datetime.now(IST)
                    ts = now.isoformat(timespec="seconds")
                    cycle = str(getattr(self, "last_cycle_started_at", None) or ts)
                    side = ""
                    for candidate_side in ("CALL", "PUT"):
                        candidate = getattr(self, "candidate_watchlist", {}).get(f"{name}:{candidate_side}", {})
                        if candidate.get("status") in {"CONFIRMED", "INVALIDATED", "NEAR_THRESHOLD"}:
                            side = candidate_side
                    event_id = hashlib.sha256(
                        f"{cycle}|{name}|{side}|{key}|{detail}".encode("utf-8")
                    ).hexdigest()
                    recorder(event_id, ts, now.date().isoformat(), name, side, "stage2/3", key, detail)
            except Exception:
                # Rejection analytics must never interrupt the live signal pipeline.
                log.exception("could not persist rejected candidate | symbol=%s | reason=%s", name, key)
        log.info("skip %s: %s %s", name, why, detail)
        return None

    def ingest_live_tick(self, instrument_key: str, tick, received_at):
        """Aggregate ticks and throttle indicator work off the websocket receive loop."""
        if not cfg.LIVE_INTRABAR_ENABLED:
            return
        und = self.underlying_by_key.get(instrument_key)
        if und is None:
            fut_und = self.underlying_by_fut_key.get(instrument_key)
            if fut_und is not None:
                try:
                    self._fut_volume_snapshot[fut_und.name] = float(tick.volume or 0.0)
                except (TypeError, ValueError):
                    pass
            return
        try:
            now = datetime.fromtimestamp(float(received_at), tz=IST) if isinstance(received_at, (int, float)) else received_at
            cumulative_volume = tick.volume
            if und.kind == "INDEX" and self._fut_volume_snapshot.get(und.name, 0.0) > 0:
                cumulative_volume = self._fut_volume_snapshot[und.name]
            partial = self.live_engine.update(und.name, tick.ltp, cumulative_volume, now)
            cached = self.live_context.get(und.name)
            if partial is None or not cached:
                return
            context_ts = datetime.fromisoformat(cached["updated_at"])
            if context_ts.tzinfo is None:
                context_ts = context_ts.replace(tzinfo=IST)
            context_age = (now - context_ts).total_seconds()
            max_context_age = cfg.SCAN_MINUTES * 60 + max(0, int(cfg.MAX_CANDLE_LAG_SEC))
            if context_age < -5 or context_age > max_context_age:
                return
            # One evaluation per symbol per second is enough for an intrabar
            # trigger and prevents high-frequency ticks from flooding workers.
            previous = self._live_eval_at.get(und.name)
            if previous is not None and (now - previous).total_seconds() < 1.0:
                return
            self._live_eval_at[und.name] = now
            # ThreadPoolExecutor uses an unbounded queue. Coalesce high-frequency
            # ticks so a busy market cannot enqueue thousands of stale evaluations.
            with self._live_state_lock:
                if getattr(self, "_closed", False) or und.name in self._live_pending:
                    return
                self._live_pending.add(und.name)
            try:
                future = self.live_pool.submit(self._detect_live_candidate, und, partial, now, cached)
                future.add_done_callback(lambda _f, name=und.name: self._release_live_pending(name))
            except RuntimeError as exc:
                self._release_live_pending(und.name)
                if not getattr(self, "_closed", False):
                    log.warning("intrabar dispatch unavailable for %s: %s", und.name, exc)
        except Exception:
            log.exception("intrabar tick dispatch failed for %s", und.name)

    def _release_live_pending(self, symbol: str):
        with self._live_state_lock:
            self._live_pending.discard(symbol)

    def _live_option_chain(self, und: Underlying, expiry: date, now: datetime):
        """Return a very recent chain snapshot without blocking the intrabar worker.

        A cache hit is accepted only for a short TTL. Cache misses schedule one
        background REST refresh; the current trigger attempt returns immediately
        and a subsequent fresh underlying tick can consume the completed result.
        """
        key = (und.name, expiry.isoformat())
        if getattr(self, "_closed", False):
            return None
        wall = time.monotonic()
        with self._live_chain_lock:
            cached = self._live_chain_cache.get(key)
            ttl = max(0.5, float(cfg.LIVE_CHAIN_CACHE_TTL_SEC))
            if cached and wall - cached[0] <= ttl:
                return cached[1]
            future = self._live_chain_inflight.get(key)
            if future is not None:
                if not future.done():
                    return None
                self._live_chain_inflight.pop(key, None)
                try:
                    chain = future.result()
                except Exception as exc:
                    self._live_chain_retry_after[key] = wall + max(0.5, float(cfg.LIVE_CHAIN_RETRY_SEC))
                    log.warning("async live option-chain refresh failed | symbol=%s | %s", und.name, exc)
                    return None
                if chain is not None:
                    self._live_chain_cache[key] = (wall, chain)
                    self._live_chain_retry_after.pop(key, None)
                    return chain
                # A successful future with an empty payload is still a failed
                # refresh; throttle it just like a raised REST exception.
                self._live_chain_retry_after[key] = wall + max(0.5, float(cfg.LIVE_CHAIN_RETRY_SEC))
                return None
            if wall < self._live_chain_retry_after.get(key, 0.0):
                return None
            try:
                self._live_chain_inflight[key] = self._live_chain_pool.submit(
                    self.fo.call, "option_chain", und, expiry)
            except RuntimeError:
                if not getattr(self, "_closed", False):
                    log.exception("live option-chain executor rejected work for %s", und.name)
                return None
            log.debug("scheduled async live option-chain refresh | symbol=%s | expiry=%s", und.name, expiry)
        return None

    def _detect_live_candidate(self, und, partial, now, cached):
        try:
            if cfg.V2_ENGINE_ENABLED:
                volume_map = {bar.ts: bar.v for bar in (cached.get("fut") or [])}
                decisions = evaluate_legacy_candles(
                    self.v2_engine, und.name, und.kind, "5m", cached["allc"],
                    live_candle=partial, futures_volume_by_timestamp=volume_map,
                    session_candles=cached["session"],
                    metadata={"market_bias": self.mkt_bias, "group": und.group,
                              "underlying_return_pct": ctx.session_return_pct(cached["session"]),
                              "index_return_pct": self.mkt_ret,
                              "provider": getattr(self.fo, "active_name", "UPSTOX")},
                )
                early = [d for d in decisions if d.state is V2DecisionState.EARLY]
                if not early:
                    return
                decision = max(early, key=lambda d: (d.candidate.score,
                                                     -d.candidate.strategy_priority))
                v2c = decision.candidate
                side = v2c.side.value
                entry = {
                    "symbol": und.name, "strategy": v2c.strategy, "side": side,
                    "mode": "INTRABAR", "setup_time": v2c.candle_timestamp.isoformat(),
                    "trigger_time": v2c.candle_timestamp.isoformat(),
                    "confirmation_time": now.isoformat(),
                    "confirmation_price": float(v2c.entry), "timeframe": "5m-intrabar",
                    "setup_id": v2c.setup_id,
                }
                candidate = {
                    "strategy": "V2_ENGINE", "side": side,
                    "reason": "; ".join(v2c.reasons) or v2c.strategy,
                    "setup_id": v2c.setup_id, "confirmation_time": now.isoformat(),
                    "bar": partial, "metrics": dict(v2c.metadata),
                    "v2_candidate": v2c, "v2_decision": decision,
                    "entry_candidate": entry,
                }
                key = candidate["setup_id"]
                with self._live_state_lock:
                    if key in self._live_inflight:
                        return
                    self._live_inflight.add(key)
                try:
                    if getattr(self, "_closed", False):
                        with self._live_state_lock:
                            self._live_inflight.discard(key)
                        return
                    self.live_pool.submit(self._evaluate_live_candidate, und, candidate, cached)
                except RuntimeError:
                    with self._live_state_lock:
                        self._live_inflight.discard(key)
                    if not getattr(self, "_closed", False):
                        log.exception("v2 live candidate dispatch unavailable for %s", und.name)
                return
            # SIMPLE uses its own EMA/VWAP/RSI/ADX live trigger; LEGACY keeps
            # the separate Normal detector. MEX and ORB remain independent.
            allow_sequential = (cfg.STRATEGY_MODE == "LEGACY" and
                                (und.kind == "INDEX" or und.name in NORMAL_SYMBOL_SET))
            # The configured Normal universe is independent from the wider MEX/ORB
            # stock universes. Index underlyings remain eligible for Normal.
            allow_simple = (cfg.STRATEGY_MODE != "LEGACY" and
                            (und.kind == "INDEX" or und.name in NORMAL_SYMBOL_SET))
            allow_mex = und.name in self.mex.universe
            allow_orb = und.name in self.momentum_orb.universe
            candidate = self.live_engine.detect(und, cached["session"], cached["allc"], partial, now, cfg,
                                                allow_sequential=allow_sequential,
                                                allow_mex=allow_mex, allow_orb=allow_orb,
                                                allow_simple=allow_simple)
            if not candidate:
                return
            strategy = candidate["strategy"]
            if strategy == "V7_SEQUENTIAL" and not (und.kind == "INDEX" or und.name in NORMAL_SYMBOL_SET):
                return
            if strategy == "MOMENTUM_EXPLOSION" and und.name not in self.mex.universe:
                return
            if strategy == "MOMENTUM_ORB" and und.name not in self.momentum_orb.universe:
                return
            key = candidate["setup_id"]
            with self._live_state_lock:
                if key in self._live_inflight:
                    return
                self._live_inflight.add(key)
            try:
                if getattr(self, "_closed", False):
                    with self._live_state_lock:
                        self._live_inflight.discard(key)
                    return
                self.live_pool.submit(self._evaluate_live_candidate, und, candidate, cached)
            except RuntimeError as exc:
                with self._live_state_lock:
                    self._live_inflight.discard(key)
                if not getattr(self, "_closed", False):
                    log.warning("live candidate dispatch unavailable for %s: %s", und.name, exc)
            except Exception:
                with self._live_state_lock:
                    self._live_inflight.discard(key)
                raise
        except Exception:
            log.exception("intrabar trigger evaluation failed for %s", und.name)

    def _evaluate_live_candidate(self, und, candidate: dict, cached: dict):
        """Fast path: use cached closed bars + separate partial candle, then mandatory chain/risk gates."""
        key = candidate["setup_id"]
        symbol_lock = self._live_symbol_locks.get(und.name)
        if symbol_lock is None or not symbol_lock.acquire(blocking=False):
            # This candidate was reserved by _detect_live_candidate. Always
            # release both reservations when the symbol lock is busy.
            with self._live_state_lock:
                self._live_inflight.discard(key)
            if candidate.get("v2_decision") is not None:
                self.v2_engine.release(candidate["v2_decision"])
            return
        published = False
        try:
            # A live alert already being tracked for this underlying blocks a
            # second entry until that signal is closed.
            for active_signal in self.tracker.signals():
                if active_signal.underlying == und.name and active_signal.status != "CLOSED":
                    return
            now = datetime.fromisoformat(candidate["confirmation_time"])
            partial = candidate["bar"]
            closed_session = list(cached["session"])
            completed_all = list(cached["allc"])
            side_sign = 1 if candidate["side"] == "CALL" else -1
            strategy = candidate["strategy"]
            score = (float(candidate["v2_candidate"].score) / 100.0
                     if strategy == "V2_ENGINE" else
                     0.82 if strategy in ("MOMENTUM_ORB", "MOMENTUM_EXPLOSION") else 1.0)
            vote_name = {"MOMENTUM_ORB": "MOMENTUM_ORB",
                         "MOMENTUM_EXPLOSION": "MOMENTUM_EXPLOSION",
                         "V7_SEQUENTIAL": "V7_SEQUENTIAL", "NORMAL_TREND": "NORMAL_TREND",
                         "SIMPLE_TREND": "SIMPLE_TREND", "V2_ENGINE": "V2_ENGINE"}[strategy]
            vote = Vote(vote_name, side_sign * score, candidate["reason"])
            v7_decision = None
            if strategy == "V7_SEQUENTIAL":
                v7_decision = SimpleNamespace(
                    status="UNDERLYING_SIGNAL_CONFIRMED", direction=candidate["side"],
                    metrics={**(candidate.get("metrics") or {}), "setup_type": "LIVE_INTRABAR_CONTINUATION",
                             "ema_cross_bars": 0, "vwap_mode": "closed-session-vwap",
                             "live_trigger": candidate["reason"]})
            entry = (dict(candidate.get("entry_candidate") or {})
                     if strategy == "V2_ENGINE" else
                     {k: v for k, v in candidate.items() if k != "bar"})
            entry["mode"] = "INTRABAR"
            entry["timeframe"] = "5m-intrabar"
            item = {"u": und, "ps": side_sign * score, "votes": [vote],
                    "session": closed_session, "allc": completed_all, "live_bar": partial,
                    "fut": cached.get("fut") or [],
                    "v7_decision": v7_decision,
                    "momentum_orb_metrics": candidate.get("metrics") or {}, "normal_metrics": candidate.get("metrics") or {},
                    "entry_candidate": entry,
                    "v2_candidate": candidate.get("v2_candidate"),
                    "v2_decision": candidate.get("v2_decision")}
            expiry = pick_expiry(self.master, und, now.date())
            if not expiry:
                self._reject("no eligible expiry", und.name, "live trigger")
                return
            chain = self._live_option_chain(und, expiry, now)
            if chain is None:
                self._watch(und.name, candidate["side"], partial.ts, side_sign * score,
                            "NEAR_THRESHOLD", reasons=["option-chain refresh pending; retrying on next fresh tick"], now=now)
                self._reject("option chain pending", und.name, "async refresh scheduled; no blocking REST call")
                return
            fut = list(item["fut"])
            # This worker already owns the per-symbol lock; call the locked implementation
            # directly instead of _evaluate(), which would try to acquire the same lock again.
            sig = self._evaluate_locked(item, [], now, (expiry, (chain, fut)))
            if sig:
                published = True
                self.live_engine.mark_sent(key)
                log.info("LIVE INTRABAR SIGNAL %s %s %s", und.name, candidate["side"], strategy)
        except Exception:
            log.exception("live candidate evaluation failed for %s", und.name)
        finally:
            if not published and candidate.get("v2_decision") is not None:
                self.v2_engine.release(candidate["v2_decision"])
            with self._live_state_lock:
                self._live_inflight.discard(key)
            symbol_lock.release()

    # ---------------- stage 1 ----------------
    def _required_history_bars(self) -> int:
        if cfg.V2_ENGINE_ENABLED:
            return max(220, int(cfg.V2_MIN_HISTORY_BARS))
        return int(cfg.SIMPLE_MIN_BARS) if cfg.STRATEGY_MODE != "LEGACY" else int(cfg.V7_MIN_BARS)

    @staticmethod
    def _has_expected_bar(raw, expected: datetime) -> bool:
        """True when the provider response contains the candle that opened at `expected` (or a later one)."""
        for c in raw or []:
            try:
                ts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=IST)
                if ts >= expected:
                    return True
            except (AttributeError, TypeError):
                continue
        return False

    def _fetch_until_ready(self, u: Underlying):
        """Fetch spot candles; if the just-closed candle is not published yet, wait briefly and fetch again.

        The scan runs SCAN_DELAY_SEC after the boundary. A feed that publishes the candle a few seconds later used to
        make every symbol look stale, so that candle was never evaluated. The wait is shared: once any worker sees the
        expected candle (or gives up) the others stop waiting.
        """
        raw = self.fo.call("spot_intraday", u)
        now = self._scan_now
        retries = int(cfg.CANDLE_READY_RETRIES)
        if now is None or retries <= 0:
            return raw
        step = int(cfg.SCAN_MINUTES)
        boundary = now.replace(minute=now.minute - now.minute % step, second=0, microsecond=0)
        expected = boundary - timedelta(minutes=step)           # open time of the candle that just closed
        if self._has_expected_bar(raw, expected):
            with self._ready_lock:
                self._ready_state["ready"] = True
            return raw
        deadline = boundary + timedelta(seconds=float(cfg.CANDLE_READY_MAX_WAIT_SEC))
        for attempt in range(1, retries + 1):
            with self._ready_lock:
                if self._ready_state["ready"] or self._ready_state["gave_up"]:
                    break
            if datetime.now(IST) >= deadline:
                break
            time.sleep(max(0.0, float(cfg.CANDLE_READY_WAIT_SEC)))
            raw = self.fo.call("spot_intraday", u)
            if self._has_expected_bar(raw, expected):
                with self._ready_lock:
                    self._ready_state["ready"] = True
                log.info("closed candle became available | symbol=%s | expected=%s | after_retries=%d",
                         u.name, expected.strftime("%H:%M"), attempt)
                return raw
        else:
            with self._ready_lock:
                if not self._ready_state["ready"]:
                    self._ready_state["gave_up"] = True
            log.warning("closed candle %s still missing after %d retries | symbol=%s", expected.strftime("%H:%M"), retries, u.name)
            return raw
        # Another worker already saw the candle (or gave up): one last fetch picks it up without more waiting.
        with self._ready_lock:
            ready = self._ready_state["ready"]
        if ready and not self._has_expected_bar(raw, expected):
            raw = self.fo.call("spot_intraday", u)
        return raw

    def _backfill_history(self, u: Underlying) -> None:
        """Re-fetch 5m history for a symbol whose bootstrap history is missing/short (bounded per scan and per day)."""
        if not cfg.HISTORY_BACKFILL_ENABLED:
            return
        today = (self._scan_now or datetime.now(IST)).date()
        with self._backfill_lock:
            if self._backfill_day != today:
                self._backfill_day, self._backfill_attempts, self._backfill_this_scan = today, {}, 0
            if self._backfill_attempts.get(u.name, 0) >= int(cfg.HISTORY_BACKFILL_MAX_ATTEMPTS):
                return
            if self._backfill_this_scan >= int(cfg.HISTORY_BACKFILL_PER_SCAN):
                return
            self._backfill_attempts[u.name] = self._backfill_attempts.get(u.name, 0) + 1
            self._backfill_this_scan += 1
        try:
            fetched = self.fo.call("history", u, cfg.HIST_DAYS)
        except Exception as exc:
            log.warning("history backfill failed | symbol=%s | %s", u.name, exc)
            return
        if not fetched:
            return
        merged = {c.ts: c for c in self.hist.get(u.name, [])}
        merged.update({c.ts: c for c in fetched})
        self.hist[u.name] = sorted(merged.values(), key=lambda c: c.ts)
        log.info("history backfilled | symbol=%s | bars=%d", u.name, len(self.hist[u.name]))
        if self.history_writer is not None:
            try:
                self.history_writer.submit(u.name, fetched)
            except Exception:
                log.exception("history backfill archive queue failed for %s", u.name)

    def _fetch_spot(self, u: Underlying):
        """(spot candles, futures candles | None). Index spot has no volume, so for indices the current-month future is
        fetched too: its volume turns the index "VWAP" from a time-weighted mean into a real VWAP. The same futures
        candles are reused in stage 2 (OI build-up), so this does not add a call for symbols that get that far."""
        try:
            raw = self._fetch_until_ready(u)
        except Exception as exc:
            log.warning("spot candle fetch failed | symbol=%s | %s", u.name, exc)
            return None, None
        try:
            if len(self.hist.get(u.name, [])) < self._required_history_bars():
                self._backfill_history(u)
        except Exception:
            log.exception("history backfill crashed for %s", u.name)
        fut = None
        if u.kind == "INDEX" and u.fut_key and cfg.INDEX_VWAP_FROM_FUTURES:
            try:
                fut = self.fo.call("fut_intraday", u)
            except Exception:
                fut = None
        return raw, fut

    def _simple_candidate(self, u: Underlying, session: List[Candle], allc: List[Candle], fut: List[Candle],
                          now: datetime, counts: dict, context_15m: int = 0):
        """SIMPLE mode: evaluate the EMA 9/21 + VWAP + RSI + ADX rule on the latest closed candle.

        Returns a stage-2 candidate dict, or None when there is no trigger. Diagnostics reuse the
        normal_trend_diagnostics structure so /status and the Telegram digest keep working.
        """
        diag = self.normal_trend_diagnostics
        diag["engine"] = "SIMPLE"
        result = evaluate_simple(
            session, allc,
            adx_min=float(cfg.SIMPLE_ADX_MIN),
            rsi_call_min=float(cfg.SIMPLE_RSI_CALL_MIN), rsi_call_max=float(cfg.SIMPLE_RSI_CALL_MAX),
            rsi_put_max=float(cfg.SIMPLE_RSI_PUT_MAX), rsi_put_min=float(cfg.SIMPLE_RSI_PUT_MIN),
            fresh_bars=int(cfg.SIMPLE_FRESH_BARS), min_bars=int(cfg.SIMPLE_MIN_BARS),
            min_session_bars=int(cfg.SIMPLE_MIN_SESSION_BARS),
            max_entry_extension_atr=float(cfg.SIMPLE_MAX_EXTENSION_ATR),
            context_15m=int(context_15m or 0))
        diag["scanned"] += 1
        side = int(result.get("side") or 0)
        diag["latest"][u.name] = {"reason": result.get("reason", ""), "setup_type": result.get("setup_type"),
                                  "gate_failures": list(result.get("gate_failures", [])),
                                  "metrics": dict(result.get("metrics") or {})}
        if len(diag["latest"]) > 100:
            diag["latest"].pop(next(iter(diag["latest"])))
        self.v7_diagnostics[u.name] = {
            "status": "UNDERLYING_SIGNAL_CONFIRMED" if side else "WAITING",
            "direction": "CALL" if side > 0 else "PUT" if side < 0 else None,
            "reason": result.get("reason", ""), "setup_type": result.get("setup_type"),
            "gate_results": result.get("gate_results", {}), "gate_failures": result.get("gate_failures", []),
            "metrics": result.get("metrics", {}),
            "selection": "SELECTED" if side else "NOT_TRIGGERED",
        }
        if side not in (1, -1):
            diag["no_trigger"] += 1
            for gate_name in result.get("gate_failures", []):
                diag["gate_failures"][gate_name] = diag["gate_failures"].get(gate_name, 0) + 1
            counts["insufficient_or_no_price_setup"] += 1
            why = ",".join(result.get("gate_failures", [])[:2]) or "no trigger"
            self._why[why] = self._why.get(why, 0) + 1
            self._note_symbol(u.name, "stage1", "no simple trigger", str(result.get("reason", ""))[:160])
            return None
        diag["detected"] += 1
        diag["selected"] += 1
        diag["setups"][result["setup_type"]] = diag["setups"].get(result["setup_type"], 0) + 1
        vote = Vote("SIMPLE_TREND", side * 1.0, str(result["reason"]))
        side_name = "CALL" if side > 0 else "PUT"
        latest_bar = allc[-1]
        confirm = latest_bar.ts.isoformat()
        self._watch(u.name, side_name, session[-1].ts, float(side), "WATCHING", reasons=[vote.reason], now=now)
        entry_contract = EntryCandidate(u.name, "SIMPLE_TREND", side_name, confirm, confirm, confirm,
                                        float(latest_bar.c), "5m",
                                        f"{u.name}:SIMPLE_TREND:{side_name}:{confirm}").to_dict()
        return {"u": u, "ps": float(side), "votes": [vote], "session": session, "allc": allc, "fut": fut,
                "v7_decision": None, "selected_engine": "SIMPLE_TREND",
                "normal_metrics": result.get("metrics", {}), "momentum_orb_metrics": {}, "mex_metrics": {},
                "entry_candidate": entry_contract}

    def stage1(self, now: datetime):
        self._scan_now = now
        if not hasattr(self, "_v2_preview_lock"):
            self._v2_preview_lock = threading.Lock()
            self._v2_early_previews = {}
        with self._v2_preview_lock:
            for preview_key, preview in list(self._v2_early_previews.items()):
                try:
                    preview_at = datetime.fromisoformat(preview["candle_ts"])
                    if (now - preview_at).total_seconds() > 15 * 60:
                        self._v2_early_previews.pop(preview_key, None)
                except (TypeError, ValueError, KeyError):
                    self._v2_early_previews.pop(preview_key, None)
        with self._ready_lock:
            self._ready_state = {"ready": False, "gave_up": False}
        with self._backfill_lock:
            self._backfill_this_scan = 0
        self.mex.begin_cycle()
        self.momentum_orb.begin_cycle()
        self.live_engine.begin_cycle()
        self.normal_trend_diagnostics = {
            "scanned": 0, "detected": 0, "selected": 0, "priority_superseded": 0,
            "no_trigger": 0, "setups": {}, "gate_failures": {}, "latest": {},
        }
        today, out = now.date(), []
        counts = {"universe": len(self.unds), "banned_or_event": 0, "fetch_empty_or_failed": 0,
                  "fetch_timed_out": 0, "no_closed_candles": 0, "stale_candles": 0,
                  "index_vwap_twap_fallback": 0, "insufficient_or_no_price_setup": 0,
                  "below_stage1_threshold": 0, "evaluation_errors": 0}
        todo = []
        for u in self.unds:
            if u.name in self.ban:
                counts["banned_or_event"] += 1
                self._note_symbol(u.name, "stage1", "symbol manually banned")
            elif events.symbol_event(u.name, today):
                counts["banned_or_event"] += 1
                self._note_symbol(u.name, "stage1", "symbol event filter")
            else:
                todo.append(u)
        # Process each request as soon as it completes. A slow first symbol must
        # not block already-fetched symbols (ThreadPoolExecutor.map is ordered).
        stage1_started = time.monotonic()
        timeout = max(1.0, float(cfg.STAGE1_SCAN_TIMEOUT_SEC))
        log.info("stage1 spot scan started | symbols=%d | workers=%d | upstox_rps=%.2f | timeout=%.1fs",
                 len(todo), cfg.SCAN_WORKERS, cfg.UPSTOX_RPS, timeout)
        for u, (raw, fut_raw), fetch_error in _stage1_fetch_results(
                self.pool, todo, self._fetch_spot, timeout):
            if fetch_error == "timeout":
                counts["fetch_timed_out"] += 1
                counts["fetch_empty_or_failed"] += 1
                self._note_symbol(u.name, "stage1", "spot candle request timed out", f"budget={timeout:.1f}s")
                continue
            if raw is None or not raw:
                counts["fetch_empty_or_failed"] += 1
                self._note_symbol(u.name, "stage1", "spot candle fetch empty or failed", fetch_error or "")
                continue
            session = _closed([c for c in raw if c.ts.date() == today], now)
            if not session:
                counts["no_closed_candles"] += 1
                self._note_symbol(u.name, "stage1", "no valid completed candles")
                continue
            candle_lag = (now - (session[-1].ts + timedelta(minutes=cfg.SCAN_MINUTES))).total_seconds()
            if candle_lag > max(0, cfg.MAX_CANDLE_LAG_SEC):
                counts["stale_candles"] += 1
                self._note_symbol(u.name, "stage1", "stale spot candles", f"lag={candle_lag:.0f}s")
                log.warning("stale spot candles %s: last close %.0fs ago", u.name, candle_lag)
                continue
            fut = _closed([x for x in (fut_raw or []) if x.ts.date() == today], now)
            if u.kind == "INDEX":
                session, real_vol = with_volume(session, {x.ts: x.v for x in fut}) if fut else (session, False)
                if not real_vol:
                    counts["index_vwap_twap_fallback"] += 1     # no usable futures volume: VWAP is a time-weighted mean
            allc = _closed(self.hist.get(u.name, []) + session, now)
            session = _closed(session, now)
            self.live_context[u.name] = {"session": list(session), "allc": list(allc), "fut": list(fut), "updated_at": now.isoformat()}
            if not session or not allc:
                counts["no_closed_candles"] += 1
                self._note_symbol(u.name, "stage1", "insufficient completed candle history")
                continue
            self._archive(u.name, session)
            if u.name == "NIFTY":
                try:
                    self.mkt_bias = htf_bias(allc, cfg.HTF_MINUTES)
                    self.mkt_ret = ctx.session_return_pct(session)
                except Exception:
                    log.exception("NIFTY market-bias update failed; keeping the previous value")
            if cfg.V2_ENGINE_ENABLED:
                try:
                    volume_map = {bar.ts: bar.v for bar in fut} if fut else {}
                    decisions = evaluate_legacy_candles(
                        self.v2_engine, u.name, u.kind, "5m", allc,
                        futures_volume_by_timestamp=volume_map,
                        session_candles=session,
                        metadata={"market_bias": self.mkt_bias, "group": u.group,
                                  "underlying_return_pct": ctx.session_return_pct(session),
                                  "index_return_pct": self.mkt_ret,
                                  "provider": getattr(self.fo, "active_name", "UPSTOX")},
                    )
                    eligible = [d for d in decisions if d.state in (
                        V2DecisionState.CONFIRMED, V2DecisionState.EARLY)]
                    if not eligible:
                        counts["insufficient_or_no_price_setup"] += 1
                        for decision in decisions:
                            for reason in decision.rejected_reasons:
                                self._why[reason] = self._why.get(reason, 0) + 1
                        self._note_symbol(u.name, "v2", "no qualifying unified setup")
                        continue
                    decision = max(eligible, key=lambda d: (d.candidate.score,
                                                            -d.candidate.strategy_priority))
                    candidate = decision.candidate
                    side_name = candidate.side.value
                    side_sign = 1 if side_name == "CALL" else -1
                    score_fraction = candidate.score / 100.0
                    reason = "; ".join(candidate.reasons) or candidate.strategy
                    vote = Vote("V2_ENGINE", side_sign * score_fraction, reason)
                    confirm = candidate.candle_timestamp.isoformat()
                    entry_contract = {
                        "symbol": u.name, "strategy": candidate.strategy,
                        "side": side_name,
                        "mode": "INTRABAR" if candidate.candle_state.value == "INTRABAR" else "CLOSED_BAR",
                        "setup_time": confirm, "trigger_time": confirm,
                        "confirmation_time": confirm,
                        "confirmation_price": float(candidate.entry),
                        "timeframe": "5m", "setup_id": candidate.setup_id,
                    }
                    self._watch(u.name, side_name, candidate.candle_timestamp,
                                side_sign * score_fraction, "WATCHING",
                                reasons=list(candidate.reasons), now=now)
                    out.append({
                        "u": u, "ps": side_sign * score_fraction, "votes": [vote],
                        "session": session, "allc": allc, "fut": fut,
                        "v7_decision": None, "selected_engine": "V2_ENGINE",
                        "normal_metrics": {}, "momentum_orb_metrics": {},
                        "mex_metrics": {}, "entry_candidate": entry_contract,
                        "v2_candidate": candidate, "v2_decision": decision,
                    })
                    counts["v2_candidates"] = counts.get("v2_candidates", 0) + 1
                except Exception as exc:
                    counts["evaluation_errors"] += 1
                    self._note_symbol(u.name, "v2", "unified engine evaluation error",
                                      f"{type(exc).__name__}: {exc}"[:160])
                    log.exception("v2 strategy evaluation failed | symbol=%s", u.name)
                continue
            # Keep the configured Normal engine exactly as selected, then run
            # MEX and ORB independently for their own universes in BOTH modes.
            if cfg.STRATEGY_MODE != "LEGACY":
                normal_eligible = u.kind == "INDEX" or u.name in NORMAL_SYMBOL_SET
                if normal_eligible:
                    try:
                        simple_cand = self._simple_candidate(
                            u, session, allc, fut, now, counts,
                            context_15m=htf_bias(allc, cfg.HTF_MINUTES))
                    except Exception as exc:       # one bad symbol must never abort the whole scan
                        counts["evaluation_errors"] += 1
                        self._note_symbol(u.name, "stage1", "simple rule evaluation error",
                                          f"{type(exc).__name__}: {exc}"[:160])
                        log.exception("simple rule evaluation failed | symbol=%s", u.name)
                        simple_cand = None
                    simple_diag = self.normal_trend_diagnostics.get("latest", {}).get(u.name, {})
                    normal_result = {
                        "side": int(simple_cand.get("ps") or 0) if simple_cand else 0,
                        "score": abs(float(simple_cand.get("ps") or 0.0)) if simple_cand else 0.0,
                        "setup_type": simple_diag.get("setup_type") or "SIMPLE_TREND",
                        "reason": simple_diag.get("reason", "no simple trigger"),
                        "metrics": dict(simple_diag.get("metrics") or {}),
                        "gate_results": {},
                        "gate_failures": list(simple_diag.get("gate_failures") or []),
                    }
                else:
                    # Do not silently expand Normal beyond its configured fixed list.
                    # MEX and ORB still run below on their own independent universes.
                    normal_result = {
                        "side": 0, "score": 0.0, "setup_type": None,
                        "reason": "outside normal universe", "metrics": {},
                        "gate_results": {}, "gate_failures": ["outside_normal_universe"],
                    }
            else:
                # Legacy Normal Trend indicator implementation is untouched.
                normal_eligible = u.kind == "INDEX" or u.name in NORMAL_SYMBOL_SET
                from strategies.normal_trend import evaluate_normal_trend
                normal_result = evaluate_normal_trend(
                    session, allc,
                    adx_min=25.0 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_ADX_MIN),
                    adx_established=25.0 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_ADX_ESTABLISHED),
                    di_min_gap=0.0 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_DI_MIN_GAP),
                    rsi_long_min=50.0 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_RSI_LONG_MIN),
                    rsi_short_max=50.0 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_RSI_SHORT_MAX),
                    candle_body_min=0.25 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_CANDLE_BODY_MIN),
                    close_pos_min=0.55 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_CLOSE_POS_MIN),
                    pullback_atr=float(cfg.NORMAL_PULLBACK_ATR),
                    max_entry_extension_atr=1.50 if cfg.V3_PROFILE_ENABLED else float(cfg.NORMAL_MAX_EXTENSION_ATR),
                    breakout_lookback=int(cfg.NORMAL_BREAKOUT_LOOKBACK),
                    rvol_min=1.2 if cfg.V3_PROFILE_ENABLED else 0.0,
                ) if normal_eligible else {
                    "side": 0, "score": 0.0, "setup_type": None,
                    "reason": "outside normal universe", "metrics": {},
                    "gate_results": {}, "gate_failures": [],
                }
                if normal_eligible:
                    normal_context_15m = htf_bias(allc, cfg.HTF_MINUTES)
                    normal_result.setdefault("metrics", {})["context_15m"] = normal_context_15m
                    # Reject a V3 Normal candidate in Stage 1, before spending
                    # option-chain API budget. Stage 3 repeats this check as a
                    # final defense against state/data changes between stages.
                    normal_side = int(normal_result.get("side") or 0)
                    if (cfg.V3_PROFILE_ENABLED and normal_side in (1, -1)
                            and not _normal_15m_context_matches(
                                "CALL" if normal_side > 0 else "PUT",
                                normal_context_15m, allow_neutral=False)):
                        required = "bullish" if normal_side > 0 else "bearish"
                        normal_result["side"] = 0
                        normal_result["reason"] = (
                            f"Normal candidate rejected in Stage 1: completed 15m context is not {required}"
                        )
                        normal_result.setdefault("gate_failures", []).append("15m_context_directional_mismatch")
                    diag = self.normal_trend_diagnostics
                    diag["scanned"] += 1
                    if int(normal_result.get("side") or 0) in (1, -1):
                        diag["detected"] += 1
                        setup_name = str(normal_result.get("setup_type") or "UNKNOWN")
                        diag["setups"][setup_name] = diag["setups"].get(setup_name, 0) + 1
                    else:
                        diag["no_trigger"] += 1
                        for gate_name in normal_result.get("gate_failures", []):
                            diag["gate_failures"][gate_name] = diag["gate_failures"].get(gate_name, 0) + 1
                    diag["latest"][u.name] = {
                        "reason": normal_result.get("reason", ""),
                        "setup_type": normal_result.get("setup_type"),
                        "gate_failures": list(normal_result.get("gate_failures", [])),
                        "metrics": dict(normal_result.get("metrics") or {}),
                    }
                    if len(diag["latest"]) > 100:
                        diag["latest"].pop(next(iter(diag["latest"])))
            # Independent strategy detectors run regardless of Normal mode.
            mex_result = self.mex.scan(u.name, session, allc)
            mex_sgn, mex_score = mex_result.get("sgn"), mex_result.get("score")
            orb_result = self.momentum_orb.scan(u.name, session, allc)
            orb_vote = None
            if orb_result.get("confirmed_now"):
                orb_side = int(orb_result["side"])
                orb_vote = Vote("MOMENTUM_ORB", orb_side * min(1.0, float(orb_result["score"]) / 100.0), str(orb_result["reason"]))
                counts["momentum_orb_confirmed"] = counts.get("momentum_orb_confirmed", 0) + 1
            mex_vote = None
            if mex_sgn in (1, -1):
                mex_vote = Vote("MOMENTUM_EXPLOSION", int(mex_sgn) * min(1.0, float(mex_score or 0.0) / 100.0), str(mex_result.get("reason") or "Momentum expansion"))
                counts["mex_confirmed"] = counts.get("mex_confirmed", 0) + 1
            normal_vote = None
            if int(normal_result.get("side") or 0) in (1, -1):
                normal_name = "SIMPLE_TREND" if cfg.STRATEGY_MODE != "LEGACY" else "NORMAL_TREND"
                normal_vote = Vote(normal_name, int(normal_result["side"]),
                                   str(normal_result.get("reason") or "Normal trend setup"))
            # One signal per underlying/candle; simultaneous setups use this deterministic priority.
            if orb_vote is not None:
                votes, selected_engine = [orb_vote], "MOMENTUM_ORB"
                if normal_vote is not None:
                    self.normal_trend_diagnostics["priority_superseded"] += 1
                    if normal_vote.name == "SIMPLE_TREND":
                        self.normal_trend_diagnostics["selected"] = max(
                            0, self.normal_trend_diagnostics["selected"] - 1)
            elif mex_vote is not None:
                votes, selected_engine = [mex_vote], "MOMENTUM_EXPLOSION"
                if normal_vote is not None:
                    self.normal_trend_diagnostics["priority_superseded"] += 1
                    if normal_vote.name == "SIMPLE_TREND":
                        self.normal_trend_diagnostics["selected"] = max(
                            0, self.normal_trend_diagnostics["selected"] - 1)
            elif normal_vote is not None:
                votes, selected_engine = [normal_vote], normal_vote.name
                # SIMPLE selection is counted inside _simple_candidate;
                # LEGACY Normal Trend is counted here.
                if normal_vote.name == "NORMAL_TREND":
                    self.normal_trend_diagnostics["selected"] += 1
            else:
                votes, selected_engine = [], None
                self._note_symbol(u.name, "stage1", "no independent engine trigger", str(normal_result.get("reason") or "no normal setup"))
            self.v7_diagnostics[u.name] = {
                "status": "UNDERLYING_SIGNAL_CONFIRMED" if normal_vote else "WAITING",
                "direction": "CALL" if normal_vote and normal_vote.score > 0 else "PUT" if normal_vote else None,
                "reason": normal_result.get("reason", ""),
                "setup_type": normal_result.get("setup_type"),
                "gate_results": normal_result.get("gate_results", {}),
                "gate_failures": normal_result.get("gate_failures", []),
                "metrics": normal_result.get("metrics", {}),
                "selection": "SELECTED" if normal_vote and selected_engine == normal_vote.name else
                             "SUPERSEDED_BY_" + str(selected_engine) if normal_vote and selected_engine else
                             "NOT_TRIGGERED",
            }
            ps = price_score(votes)
            if not votes:
                counts["insufficient_or_no_price_setup"] += 1
                why = price_diagnose(session, allc)
                self._why[why] = self._why.get(why, 0) + 1
                self._note_symbol(u.name, "stage1", "no price setup", why)
            elif abs(ps) > 0.0:
                side = "CALL" if ps > 0 else "PUT"
                self._watch(u.name, side, session[-1].ts, ps, "WATCHING", reasons=[v.reason for v in votes], now=now)
                latest_bar = allc[-1]
                if orb_vote is not None:
                    strategy_name = "MOMENTUM_ORB"
                    setup_time = orb_result.get("setup_bar")
                    trigger_time = setup_time
                    confirmation_time = orb_result.get("confirmed_bar")
                elif mex_vote is not None:
                    strategy_name = "MOMENTUM_EXPLOSION"
                    setup_time = latest_bar.ts.isoformat()
                    trigger_time = latest_bar.ts.isoformat()
                    confirmation_time = trigger_time
                elif selected_engine == "MOMENTUM_EXPLOSION":
                    strategy_name = "MOMENTUM_EXPLOSION"
                    setup_time = latest_bar.ts.isoformat()
                    trigger_time = latest_bar.ts.isoformat()
                    confirmation_time = trigger_time
                elif selected_engine == "SIMPLE_TREND":
                    strategy_name = "SIMPLE_TREND"
                    setup_time = latest_bar.ts.isoformat()
                    trigger_time = latest_bar.ts.isoformat()
                    confirmation_time = trigger_time
                else:
                    strategy_name = "NORMAL_TREND"
                    setup_time = latest_bar.ts.isoformat()
                    trigger_time = latest_bar.ts.isoformat()
                    confirmation_time = trigger_time
                entry_contract = EntryCandidate(u.name, strategy_name, side, str(setup_time), str(trigger_time),
                    str(confirmation_time), float(latest_bar.c), "5m",
                    f"{u.name}:{strategy_name}:{side}:{confirmation_time}").to_dict()
                if selected_engine == "MOMENTUM_EXPLOSION":
                    entry_contract["metrics"] = mex_result.get("metrics", {})
                out.append({"u": u, "ps": ps, "votes": votes, "session": session, "allc": allc, "fut": fut,
                            "v7_decision": None, "selected_engine": selected_engine, "normal_metrics": normal_result.get("metrics", {}), "momentum_orb_metrics": orb_result.get("metrics", {}), "mex_metrics": mex_result.get("live", {}).get("metrics", {}),
                            "entry_candidate": entry_contract})
        log.info("stage1 scan complete | elapsed=%.1fs | universe=%d | fetch_failed=%d | fetch_timed_out=%d | stale=%d | no_setup=%d | candidates=%d",
                 time.monotonic() - stage1_started, counts["universe"], counts["fetch_empty_or_failed"],
                 counts["fetch_timed_out"], counts["stale_candles"],
                 counts["insufficient_or_no_price_setup"], len(out))
        # A setup that no longer refreshes for 15 minutes expires from active monitoring.
        with self._candidate_lock:
            for item in self.candidate_watchlist.values():
                if item.get("status") in {"SIGNAL_SENT", "INVALIDATED"}:
                    continue
                try:
                    updated = datetime.fromisoformat(item["updated_at"])
                    if (now - updated).total_seconds() > 15 * 60:
                        item["status"] = "INVALIDATED"
                        item["reasons"] = list(item.get("reasons", [])) + ["candidate expired after 15 minutes without a fresh trigger"]
                except (TypeError, ValueError, KeyError):
                    item["status"] = "INVALIDATED"
                    item["reasons"] = list(item.get("reasons", [])) + ["candidate timestamp invalid"]
        self._schedule_candidate_watchlist_persistence()
        # A V7 confirmation must not be starved by identical +/-1 legacy vote scores.
        # Keep all candidates (the universe is already bounded by the liquidity scanner);
        # stage-2 calls remain concurrency/rate-limiter controlled.
        def candidate_rank(item):
            decision = item.get("v7_decision")
            confirmed = bool(decision and decision.status == "UNDERLYING_SIGNAL_CONFIRMED")
            metrics = decision.metrics if confirmed else {}
            try:
                adx_strength = float(metrics.get("adx") or 0.0)
            except (TypeError, ValueError):
                adx_strength = 0.0
            return (0 if confirmed else 1, -adx_strength, -abs(item["ps"]), item["u"].name)
        out.sort(key=candidate_rank)
        self._stage1_diagnostics = counts
        counts["stage1_candidates_forwarded"] = len(out)
        counts["stage1_candidates_dropped"] = 0
        return out, len(out)

    # ---------------- stage 2 prefetch ----------------
    def _fetch_stage2(self, u: Underlying, expiry: date, now: datetime, fut_pre=None):
        chain, fut = None, list(fut_pre or [])
        try:
            chain = self.fo.call("option_chain", u, expiry)
        except Exception as e:
            log.warning("chain failed %s: %s", u.name, e)
        return chain, fut

    def _prefetch(self, cands, now: datetime) -> dict:
        """Fetch candidate chains within one bounded wall-clock budget.

        Timed-out candidates are explicitly marked with a None chain so _evaluate
        cannot silently issue another synchronous request and stall the scan.
        """
        jobs = {}
        for c in cands:
            u = c["u"]
            expiry = pick_expiry(self.master, u, now.date())
            if expiry:
                future = self.pool.submit(self._fetch_stage2, u, expiry, now, c.get("fut"))
                jobs[future] = (u.name, expiry)
        timeout = max(1.0, float(cfg.STAGE2_PREFETCH_TIMEOUT_SEC))
        results = {}
        self.prefetch_diagnostics = {}
        # Consume completions in arrival order. Waiting on futures in candidate
        # order let one slow early symbol consume the entire budget while later
        # chains had already completed and were incorrectly marked timed out.
        try:
            for future in as_completed(jobs, timeout=timeout):
                name, expiry = jobs[future]
                try:
                    results[name] = (expiry, future.result())
                    self.prefetch_diagnostics[name] = "completed"
                except Exception as exc:
                    results[name] = (expiry, (None, []))
                    self.prefetch_diagnostics[name] = f"error:{type(exc).__name__}"
                    log.warning("option-chain prefetch failed | symbol=%s | %s", name, exc)
        except FutureTimeoutError:
            pass
        for future, (name, expiry) in jobs.items():
            if name in results:
                continue
            future.cancel()
            results[name] = (expiry, (None, []))
            self.prefetch_diagnostics[name] = "timeout"
            log.warning("option-chain prefetch timed out | symbol=%s | budget=%.1fs", name, timeout)
        return results

    # ---------------- stage 2 + 3 ----------------
    def _v2_status(self) -> dict:
        diagnostics = getattr(self, "_stage1_diagnostics", {}) or {}
        preview_lock = getattr(self, "_v2_preview_lock", None)
        if preview_lock is not None:
            with preview_lock:
                preview_count = len(getattr(self, "_v2_early_previews", {}) or {})
        else:
            preview_count = 0
        return {
            "enabled": bool(cfg.V2_ENGINE_ENABLED),
            "profile": "BALANCED_HYBRID",
            "strategy_count": 9,
            "min_score": float(cfg.V2_MIN_SCORE),
            "min_adx": 22.0,
            "max_entry_extension_atr": 1.25,
            "min_risk_reward": float(cfg.V2_MIN_RR),
            "min_history_bars": max(220, int(cfg.V2_MIN_HISTORY_BARS)),
            "intrabar_enabled": bool(cfg.LIVE_INTRABAR_ENABLED),
            "closed_bar_candidates": int(diagnostics.get("v2_candidates", 0)),
            "early_previews_cached": preview_count,
            "signal_only": True,
        }

    def run_once(self, now: datetime) -> List[Signal]:
        """One scan cycle. `last_cycle_at` is set only when the cycle completes without raising, so health
        checks can tell a finished scan from a crashed one."""
        t0 = time.monotonic()
        self.last_cycle_started_at = datetime.now(IST).isoformat(timespec="seconds")
        self.last_cycle_error = None
        self.last_cycle_stage_seconds = {}
        self._cycle_stage_started = t0
        self.last_cycle_stage = "starting"
        log.info("scan cycle started | at=%s | universe=%d", self.last_cycle_started_at, len(self.unds))
        try:
            out = self._run_once(now)
            self._set_cycle_stage("completed")
            self.last_cycle_seconds = round(time.monotonic() - t0, 3)
            self.last_cycle_at = datetime.now(IST).isoformat(timespec="seconds")
            log.info("scan cycle completed | duration=%.3fs | candidates=%d | signals=%d",
                     self.last_cycle_seconds, int((getattr(self, "last_stats", {}) or {}).get("kept", 0)), len(out))
            return out
        except Exception as exc:
            failed_stage = self.last_cycle_stage
            self._set_cycle_stage("failed")
            self.last_cycle_error = f"{type(exc).__name__}: {exc}"[:500]
            self.last_cycle_seconds = round(time.monotonic() - t0, 3)
            log.exception("scan cycle failed | stage=%s | duration=%.3fs", failed_stage, self.last_cycle_seconds)
            raise

    def _run_once(self, now: datetime) -> List[Signal]:
        today = now.date()
        self._rej = {}
        self._why = {}
        self._symbol_rejections = {}
        self.v7_diagnostics = {}
        self.normal_trend_diagnostics = {
            "scanned": 0, "detected": 0, "selected": 0, "priority_superseded": 0,
            "no_trigger": 0, "setups": {}, "gate_failures": {}, "latest": {},
        }
        self.live_engine.begin_cycle()
        # Pending Telegram retries are maintenance work. In production they use
        # a dedicated database client on a daemon worker, never the scan thread or
        # the primary DB connection/lock.
        if getattr(self.db, "backend", None) == "postgres":
            self._schedule_pending_outbox_delivery()
        else:
            # Keep deterministic behavior for injected in-memory test databases.
            self._set_cycle_stage("telegram_outbox")
            self._deliver_pending_outbox()
        if not can_signal(now):
            self.last_stats = {
                "ts": now.isoformat(timespec="seconds"),
                "stage1_hits": 0, "kept": 0, "signals": 0,
                "rejects": {"outside_signal_window": 1},
                "no_setup_reasons": {},
                "candidate_watchlist": list(self.candidate_watchlist.values())[-100:],
                "symbol_diagnostics": self._symbol_rejections,
                "mex_engine": self.mex.snapshot(), "momentum_orb_engine": self.momentum_orb.snapshot(), "v7_sequential": self.v7_diagnostics,
                "normal_trend_engine": self.normal_trend_diagnostics,
                "v2_engine": self._v2_status(),
                "normal_live_intrabar": self.live_engine.normal_snapshot(),
                "v7_config": {"adx_min": cfg.V7_ADX_MIN, "cross_max_bars": cfg.V7_CROSSOVER_MAX_BARS,
                              "vwap_max_atr": cfg.V7_VWAP_MAX_ATR, "pullback_trend_bars": cfg.V7_PULLBACK_TREND_BARS,
                              "pullback_touch_atr": cfg.V7_PULLBACK_TOUCH_ATR},
            }
            log.info("scan skipped outside configured NSE signal window: %s", now.isoformat(timespec="seconds"))
            return []
        self._set_cycle_stage("stage1_underlying_scan")
        cands, n_hits = self.stage1(now)
        self.last_stats = {"ts": now.isoformat(timespec="seconds"), "stage1_hits": n_hits, "kept": len(cands), "signals": 0,
                           "mkt_bias": self.mkt_bias, "stage1_diagnostics": getattr(self, "_stage1_diagnostics", {}),
                           "rejects": self._rej, "no_setup_reasons": self._why,
                           "symbol_diagnostics": self._symbol_rejections,
                           "mex_engine": self.mex.snapshot(), "momentum_orb_engine": self.momentum_orb.snapshot(), "v7_sequential": self.v7_diagnostics,
                           "normal_trend_engine": self.normal_trend_diagnostics,
                           "v2_engine": self._v2_status(),
                           "normal_live_intrabar": self.live_engine.normal_snapshot(),
                           "v7_config": {"adx_min": cfg.V7_ADX_MIN, "cross_max_bars": cfg.V7_CROSSOVER_MAX_BARS,
                                         "vwap_max_atr": cfg.V7_VWAP_MAX_ATR, "pullback_trend_bars": cfg.V7_PULLBACK_TREND_BARS,
                                         "pullback_touch_atr": cfg.V7_PULLBACK_TOUCH_ATR}}
        if not cands:
            self.last_stats["momentum_orb_engine"] = self.momentum_orb.snapshot()
            self.last_stats["mex_engine"] = self.mex.snapshot()
            self.last_stats["candidate_watchlist"] = list(self.candidate_watchlist.values())[-100:]
            return []
        self._set_cycle_stage("stage2_option_chain_prefetch")
        vix_f = self.pool.submit(lambda: self.fo.call("vix_intraday"))
        pre = self._prefetch(cands, now)
        vix = []
        try:
            vix = _closed([c for c in vix_f.result(timeout=max(1.0, float(cfg.VIX_FETCH_TIMEOUT_SEC))) if c.ts.date() == today], now)
        except FutureTimeoutError:
            vix_f.cancel()
            log.warning("India VIX fetch timed out; continuing without VIX context")
        except Exception:
            log.exception("India VIX fetch failed; continuing without VIX context")
        self._set_cycle_stage("stage3_candidate_evaluation")
        sent = []
        eval_errors = 0
        for c in cands:                                   # every setup that passes the strict gates is evaluated
            try:
                sig = self._evaluate(c, vix, now, pre.get(c["u"].name))
            except Exception as exc:
                # One failing candidate (odd quote, provider field, DB hiccup) used to abort the whole scan, so the
                # candidates behind it never got an alert and the same crash repeated on every retry of that candle.
                eval_errors += 1
                name = c["u"].name
                log.exception("candidate evaluation failed | symbol=%s", name)
                self._reject("evaluation error", name, f"{type(exc).__name__}: {exc}"[:160])
                continue
            if sig:
                sent.append(sig)
        self.last_stats["evaluation_errors"] = eval_errors
        self.last_stats["signals"] = len(sent)
        self.last_stats["normal_trend_engine"] = self.normal_trend_diagnostics
        self.last_stats["v2_engine"] = self._v2_status()
        self.last_stats["normal_live_intrabar"] = self.live_engine.normal_snapshot()
        self.last_stats["mex_engine"] = self.mex.snapshot()
        self.last_stats["momentum_orb_engine"] = self.momentum_orb.snapshot()
        self.last_stats["option_chain_prefetch"] = dict(getattr(self, "prefetch_diagnostics", {}))
        self.last_stats["candidate_watchlist"] = list(self.candidate_watchlist.values())[-100:]
        self.last_stats["symbol_diagnostics"] = self._symbol_rejections
        return sent

    def _already_alerted(self, key: str) -> bool:
        """Persisted per-candle de-dup: a restart or a retried scan must not send the same alert twice."""
        try:
            return self.db.kv_get(key) is not None
        except Exception:
            return False

    def _mark_alerted(self, key: str, db=None):
        store = db or self.db
        try:
            store.kv_set(key, "1")
        except Exception:
            log.exception("could not persist alert de-dup key")

    def _maintenance_connection(self):
        """Create one isolated database client shared only by background maintenance workers."""
        with self._maintenance_db_lock:
            # Injected in-memory databases are deliberately shared by test workers;
            # opening a second production PostgreSQL connection would be invalid.
            db = getattr(self, "db", None)
            if getattr(db, "_test_injected", False):
                return db
            if self._outbox_db is None:
                from db import DB
                self._outbox_db = DB(initialize_schema=False)
            return self._outbox_db

    def _schedule_candidate_watchlist_persistence(self):
        """Coalesce candidate-state writes and persist them away from Stage 1/2."""
        if getattr(self, "_closed", False):
            return
        with self._candidate_persist_lock:
            self._candidate_persist_dirty = True
            thread = self._candidate_persist_thread
            if thread is not None and thread.is_alive():
                return
            started_at = datetime.now(IST).isoformat(timespec="seconds")
            self._candidate_persist_status = {
                "state": "running", "started_at": started_at, "finished_at": None, "error": None,
            }

            def _worker():
                error = None
                try:
                    while True:
                        with self._candidate_persist_lock:
                            self._candidate_persist_dirty = False
                            with self._candidate_lock:
                                payload = __import__("json").dumps(self.candidate_watchlist)
                        self._maintenance_connection().kv_set("candidate_watchlist_v1", payload)
                        with self._candidate_persist_lock:
                            if self._candidate_persist_dirty and not getattr(self, "_closed", False):
                                continue
                            break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:300]
                    log.exception("background candidate watchlist persistence failed")
                with self._candidate_persist_lock:
                    self._candidate_persist_status = {
                        "state": "failed" if error else "completed",
                        "started_at": started_at,
                        "finished_at": datetime.now(IST).isoformat(timespec="seconds"),
                        "error": error,
                    }

            self._candidate_persist_thread = threading.Thread(
                target=_worker, name="candidate-watchlist-persist", daemon=True
            )
            self._candidate_persist_thread.start()

    def _schedule_pending_outbox_delivery(self):
        """Retry persisted Telegram alerts on an isolated DB connection off the scan thread."""
        if getattr(self, "_closed", False):
            return
        with self._outbox_lock:
            thread = self._outbox_thread
            if thread is not None and thread.is_alive():
                return
            started_at = datetime.now(IST).isoformat(timespec="seconds")
            self._outbox_status = {"state": "running", "started_at": started_at, "finished_at": None, "error": None}

            def _worker():
                error = None
                try:
                    self._deliver_pending_outbox(db=self._maintenance_connection())
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:300]
                    log.exception("background Telegram outbox retry failed")
                with self._outbox_lock:
                    self._outbox_status = {
                        "state": "failed" if error else "completed",
                        "started_at": started_at,
                        "finished_at": datetime.now(IST).isoformat(timespec="seconds"),
                        "error": error,
                    }

            self._outbox_thread = threading.Thread(
                target=_worker, name="telegram-outbox-retry", daemon=True
            )
            self._outbox_thread.start()

    def _deliver_pending_outbox(self, limit: int = 100, db=None):
        """Retry only undelivered chat rows; successful chats are never sent a duplicate."""
        store = db or self.db
        try:
            rows = store.pending_telegram(limit)
        except Exception:
            log.exception("could not read Telegram outbox")
            return
        if rows:
            log.info("telegram outbox delivery batch started | pending=%d", len(rows))
        for row in rows:
            try:
                ok = self.tg.send_to_chat(row["message"], row["chat_id"])
                if ok:
                    store.mark_telegram_delivered(row["id"])
                    status = store.telegram_delivery_status(row["dedupe_key"])
                    log.info("telegram outbox item acknowledged | signal_id=%s | chat_id=%s | delivered=%d/%d",
                             row.get("signal_id"), row.get("chat_id"),
                             status.get("delivered", 0), status.get("total", 0))
                    if status.get("all_delivered"):
                        self._mark_alerted(row["dedupe_key"], db=store)
                        log.info("telegram signal fully delivered | signal_id=%s", row.get("signal_id"))
                else:
                    error = getattr(self.tg, "last_error", None) or "Telegram did not acknowledge"
                    store.mark_telegram_failed(row["id"], error)
                    log.error("telegram outbox item remains pending | signal_id=%s | chat_id=%s | error=%s",
                              row.get("signal_id"), row.get("chat_id"), error)
            except Exception as exc:
                log.exception("Telegram outbox delivery failed for %s", row.get("id"))
                try:
                    store.mark_telegram_failed(row["id"], str(exc))
                except Exception:
                    log.exception("could not persist Telegram outbox failure")

    def _queue_signal_alert(self, sig: Signal, dedupe_key: str, message: str, now: datetime):
        """Persist before attempting delivery; an unsuccessful alert remains retryable across Render restarts."""
        # Lightweight test doubles / custom senders may expose only send(); retain
        # their synchronous acknowledgement contract. Production Telegram always has chat_ids.
        if not hasattr(self.tg, "chat_ids"):
            delivered = self.tg.send(message)
            if delivered is not False:
                self._mark_alerted(dedupe_key)
                self._watch(sig.underlying, sig.side, datetime.fromisoformat(sig.ts) - timedelta(minutes=cfg.SCAN_MINUTES),
                            1.0 if sig.side == "CALL" else -1.0, "SIGNAL_SENT", sig.confidence,
                            reasons=["Telegram sender acknowledged synchronously"], now=now)
                return True
            self._note_symbol(sig.underlying, "telegram", "synchronous delivery failed",
                              getattr(self.tg, "last_error", None) or "sender returned false")
            return False
        chats = list(getattr(self.tg, "chat_ids", []) or [])
        if not chats:
            self._note_symbol(sig.underlying, "telegram", "delivery not queued", "no Telegram chat IDs configured")
            log.error("signal %s passed all gates but Telegram chat IDs are not configured", sig.id)
            return False
        try:
            for chat_id in chats:
                self.db.enqueue_telegram(dedupe_key, sig.id, str(chat_id), message)
        except Exception as exc:
            self._note_symbol(sig.underlying, "telegram", "outbox enqueue failed",
                              f"{type(exc).__name__}: {exc}"[:180])
            log.exception("signal %s saved but Telegram outbox enqueue failed", sig.id)
            return False
        log.info("telegram alert queued | signal_id=%s | chats=%d", sig.id, len(chats))
        try:
            self._deliver_pending_outbox()
            status = self.db.telegram_delivery_status(dedupe_key)
        except Exception as exc:
            self._note_symbol(sig.underlying, "telegram", "outbox status check failed",
                              f"{type(exc).__name__}: {exc}"[:180])
            log.exception("signal %s is queued but delivery status could not be confirmed", sig.id)
            return False
        if status.get("all_delivered"):
            self._mark_alerted(dedupe_key)
            self._watch(sig.underlying, sig.side, datetime.fromisoformat(sig.ts) - timedelta(minutes=cfg.SCAN_MINUTES),
                        1.0 if sig.side == "CALL" else -1.0, "SIGNAL_SENT", sig.confidence,
                        reasons=["Telegram acknowledged by all configured chats"], now=now)
            return True
        self._note_symbol(sig.underlying, "telegram", "delivery pending",
                          f"delivered={status.get('delivered', 0)}/{status.get('total', 0)}; pending={status.get('pending', 0)}")
        log.warning("signal alert not fully delivered | signal_id=%s | delivered=%d/%d | pending=%d",
                    sig.id, status.get("delivered", 0), status.get("total", 0), status.get("pending", 0))
        return False

    def _evaluate(self, c: dict, vix: List[Candle], now: datetime, pre=None) -> Optional[Signal]:
        """Serialize all closed-bar and intrabar evaluation per underlying."""
        und = c["u"]
        lock = self._live_symbol_locks.get(und.name)
        if lock is None or not lock.acquire(blocking=False):
            return self._reject("symbol evaluation busy", und.name)
        try:
            try:
                result = self._evaluate_locked(c, vix, now, pre)
            except Exception:
                if c.get("v2_decision") is not None:
                    self.v2_engine.release(c["v2_decision"])
                raise
            if result is None and c.get("v2_decision") is not None:
                self.v2_engine.release(c["v2_decision"])
            return result
        finally:
            lock.release()

    def _evaluate_locked(self, c: dict, vix: List[Candle], now: datetime, pre=None) -> Optional[Signal]:
        u: Underlying = c["u"]
        entry_bar = c.get("live_bar") or c["session"][-1]
        today = now.date()
        side_hint = "CALL" if c["ps"] > 0 else "PUT"
        sgn = 1 if side_hint == "CALL" else -1
        if any(active.underlying == u.name and active.status != "CLOSED"
               for active in self.tracker.signals()):
            return self._reject("active underlying signal", u.name, "existing signal remains open")
        expiry = pick_expiry(self.master, u, today)
        if not expiry:
            return self._reject("no eligible expiry", u.name)
        if pre and pre[0] == expiry:
            chain, fut = pre[1]
        else:
            chain, fut = self._fetch_stage2(u, expiry, now, c.get("fut"))
        if chain is None:
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "NEAR_THRESHOLD", reasons=["waiting for fresh option-chain data"], now=now)
            return self._reject("no option chain", u.name)
        # `now` is captured at the beginning of a scan and may be tens of
        # seconds old by the time the option-chain REST request completes.
        # ChainSnapshot.ts is stamped when the provider response is received,
        # so compare it with a fresh clock reading rather than the scan-start
        # timestamp. Normalize both timestamps to IST before age/date checks.
        intrabar_mode = str((c.get("entry_candidate") or {}).get("mode", "")).upper() == "INTRABAR"
        max_chain_age = cfg.LIVE_CHAIN_MAX_AGE_SEC if intrabar_mode else cfg.MAX_OPTION_CHAIN_AGE_SEC

        def _chain_age(snapshot):
            """(age seconds, IST timestamp | None, wall-clock reading). ChainSnapshot.ts is stamped when the provider
            response arrives, so age is measured against a fresh clock, not the scan-start timestamp."""
            wall_now = datetime.now(IST)
            ts_value = getattr(snapshot, "ts", None)
            try:
                if ts_value is None:
                    return float("inf"), None, wall_now
                ts_norm = ts_value.replace(tzinfo=IST) if ts_value.tzinfo is None else ts_value.astimezone(IST)
                return (wall_now - ts_norm).total_seconds(), ts_norm, wall_now
            except (TypeError, ValueError, OverflowError, AttributeError):
                return float("inf"), None, wall_now

        def _chain_stale(age, ts_norm, wall_now):
            return (ts_norm is None or age < -5 or age > max_chain_age or ts_norm.date() != wall_now.date())

        chain_age, normalized_chain_ts, validation_now = _chain_age(chain)
        if _chain_stale(chain_age, normalized_chain_ts, validation_now) and not intrabar_mode:
            # Chains are prefetched for every candidate and then evaluated one after another (each evaluation does
            # remote DB writes), so a chain fetched early in the batch can age past the limit before its turn.
            # That is a queueing artefact, not bad data: fetch one fresh snapshot instead of dropping the signal.
            fresh_chain, _fresh_fut = self._fetch_stage2(u, expiry, now, c.get("fut"))
            if fresh_chain is not None:
                log.info("option chain re-fetched | symbol=%s | previous_age=%.1fs", u.name, chain_age)
                chain = fresh_chain
                chain_age, normalized_chain_ts, validation_now = _chain_age(chain)
        if _chain_stale(chain_age, normalized_chain_ts, validation_now):
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "NEAR_THRESHOLD", reasons=[f"option-chain data stale or timestamp unavailable (age={chain_age:.1f}s)"], now=now)
            return self._reject("stale option chain", u.name, f"age={chain_age:.1f}s")
        quality = chain_quality_issues(chain, entry_bar.c, side=side_hint)
        if quality:
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "NEAR_THRESHOLD",
                        reasons=["option-chain quality: " + "; ".join(quality)], now=now)
            return self._reject("option chain quality", u.name, "; ".join(quality))

        a5 = atr(c["allc"], 14)[-1]
        spot = chain.spot or entry_bar.c
        entry_candidate = c.get("entry_candidate")
        valid_entry, entry_reason, entry_metrics = validate_entry_candidate(
            entry_candidate, entry_bar, spot, a5,
            cfg.LIVE_MAX_ADVERSE_ATR, cfg.LIVE_MAX_RUNAWAY_ATR)
        if not valid_entry:
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "INVALIDATED",
                        reasons=[entry_reason], now=now)
            return self._reject(entry_reason, u.name, str(entry_metrics))

        # --- 15m trend context ---
        htf = htf_bias(c["allc"], cfg.HTF_MINUTES)
        is_normal_context_setup = any(
            getattr(v, "name", "") in {"SIMPLE_TREND", "NORMAL_TREND"}
            for v in c.get("votes", [])
        )
        if is_normal_context_setup and not _normal_15m_context_matches(
                side_hint, htf, allow_neutral=not bool(cfg.V3_PROFILE_ENABLED)):

            required_context = "bullish" if side_hint == "CALL" else "bearish"
            actual_context = "bullish" if htf == 1 else "bearish" if htf == -1 else "neutral/unknown"
            detail = f"Normal {side_hint} requires completed 15m {required_context} context; current 15m context is {actual_context}"
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "INVALIDATED",
                        reasons=[detail], now=now)
            return self._reject("15m normal context mismatch", u.name, detail)
        # 15m session VWAP is supporting score evidence only. Use today's closed
        # 5m session candles; the helper drops any incomplete 15m aggregate.
        htf_vwap = htf_vwap_bias(c["session"], cfg.HTF_MINUTES)
        # A counter-trend reversal is specifically designed to turn against the prior trend.
        # Keep the higher-timeframe opposition penalty in the 10-point score, but do not
        # apply the optional trend-following hard veto to a validated REVERSAL setup.
        is_reversal_setup = any(getattr(v, "name", "") == "REVERSAL" for v in c.get("votes", []))
        # OI and option-chain direction are not used to approve/reject a signal. The chain is
        # still required to choose a real option contract and validate executable quotes.
        prev_close = self.hist.get(u.name, [])[-1].c if self.hist.get(u.name) else None
        is_exp = today in self.master.expiries.get(u.name, [])
        reg = calc_regime(c["session"], prev_close, vix, c["allc"], today, is_exp)

        # Independent context (15m / NIFTY / relative strength / same-time-of-day volume / OI wall). These are not
        # derived from the signal candle's own indicators, so they add information instead of repeating it.
        cfeat = ctx.compute_features(u.kind, side_hint, c["allc"], c["session"], chain, a5, spot, htf,
                                     self.mkt_bias, self.mkt_ret, c.get("fut") or fut)
        # V7 sequential confirmation is the authoritative underlying setup. Keep
        # independent context gates visible, but do not let optional context filters
        # (relative strength, time-of-day volume, OI wall, futures OI) silently veto it.
        # Contract validity, executable quotes, live-price invalidation and risk checks
        # below remain mandatory.
        is_v7_sequential = bool(c.get("v7_decision") and
                                c["v7_decision"].status == "UNDERLYING_SIGNAL_CONFIRMED")
        is_momentum_orb = any(getattr(v, "name", "") == "MOMENTUM_ORB" for v in c.get("votes", []))
        is_mex = any(getattr(v, "name", "") == "MOMENTUM_EXPLOSION" for v in c.get("votes", []))
        is_normal_trend = any(getattr(v, "name", "") == "NORMAL_TREND" for v in c.get("votes", []))
        is_simple = any(getattr(v, "name", "") == "SIMPLE_TREND" for v in c.get("votes", []))
        is_v2 = c.get("v2_candidate") is not None
        context_veto = ctx.veto_from(cfeat, u.kind, is_reversal_setup, ignore_trend_context=True)
        if context_veto and (is_v7_sequential or is_momentum_orb or is_mex or is_normal_trend or is_simple or is_v2):
            cfeat["independent_setup_bypassed_context_veto"] = {
                "setup": "SIMPLE_TREND" if is_simple else "V7_SEQUENTIAL" if is_v7_sequential else "MOMENTUM_ORB" if is_momentum_orb else "MOMENTUM_EXPLOSION" if is_mex else "NORMAL_TREND" if is_normal_trend else "V2_ENGINE",
                "code": context_veto[0], "detail": context_veto[1]
            }
            self._note_symbol(u.name, "independent_setup", "optional context veto bypassed",
                              f"{context_veto[0]}: {context_veto[1]}")
        elif context_veto:
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "INVALIDATED",
                        reasons=[f"{context_veto[0]}: {context_veto[1]}"], now=now)
            return self._reject(context_veto[0], u.name, context_veto[1])

        sources = {"price": c["ps"], "regime": reg["score"]}
        mkt = self.mkt_bias if u.kind == "STOCK" else 0
        # OI/option-chain direction is informational only; it is not a separate alert-approval gate.
        independent_agree = 1
        v7_decision = c.get("v7_decision")
        if is_simple:
            simple_metrics = dict(c.get("normal_metrics") or {})
            adx_value = float(simple_metrics.get("adx") or cfg.SIMPLE_ADX_MIN)
            # Informational strength only (nothing is gated on it): 8.0 at the ADX threshold, +0.1 per extra ADX point.
            score_10 = round(min(10.0, 8.0 + max(0.0, adx_value - float(cfg.SIMPLE_ADX_MIN)) / 10.0), 2)
            diagnostic_metrics = dict(simple_metrics)
            diagnostic_metrics["score_components"] = {"simple_trend_rule": 1.0}
            gate = {"metrics": diagnostic_metrics}
            score_reasons = [
                str(c["votes"][0].reason),
                f"Rule: EMA9/21 direction + session VWAP side + RSI zone + ADX >= {float(cfg.SIMPLE_ADX_MIN):g}",
            ]
            score_rejects = []
            extra, extra_note = 0.0, "not applied: simple rule is authoritative"
        elif is_v7_sequential:
            # The sequential engine already passed every mandatory indicator gate.
            # Do not run a second legacy score/ADX/EMA gate over the same candle.
            v7_metrics = v7_decision.metrics
            adx_value = float(v7_metrics.get("adx") or 25.0)
            score_10 = round(min(10.0, 8.0 + max(0.0, adx_value - 25.0) / 10.0), 2)
            diagnostic_metrics = dict(v7_metrics)
            diagnostic_metrics["score_components"] = {"v7_sequential_confirmation": 1.0}
            gate = {"metrics": diagnostic_metrics}
            score_reasons = [
                ("V7 live intrabar confirmation: EMA 9/21, session VWAP, directional strength and breakout acceptance"
                 if (entry_candidate or {}).get("mode") == "INTRABAR"
                 else "V7 sequential confirmation passed: EMA 9/21, session VWAP, RSI(14), ADX(14) >= 25, directional DI and strong confirmation candle"),
                f"ADX {adx_value:.1f}; EMA cross age {v7_metrics.get('ema_cross_bars')} completed bar(s)",
                f"Setup sequence: {v7_metrics.get('setup_type', 'FRESH_EMA_CROSS')}",
                f"VWAP calculation: {v7_metrics.get('vwap_mode', 'unknown')}",
            ]
            score_rejects = []
            extra, extra_note = 0.0, "not applied: V7 sequential hard gates are authoritative"
        elif is_mex:
            mex_vote = next(v for v in c["votes"] if getattr(v, "name", "") == "MOMENTUM_EXPLOSION")
            mex_raw = abs(float(mex_vote.score)) * 100.0
            score_10 = round(min(10.0, 8.0 + 2.0 * max(0.0, mex_raw - 75.0) / 25.0), 2)
            diagnostic_metrics = {**((entry_candidate or {}).get("metrics") or {}),
                                  "live_intrabar": True, "trigger": mex_vote.reason,
                                  "score_components": {"momentum_explosion": round(mex_raw, 2)}}
            gate = {"metrics": diagnostic_metrics}
            score_reasons = [str(mex_vote.reason), "MEX live impulse trigger; next completed candle is not required"]
            score_rejects = []
            extra, extra_note = 0.0, "not applied: dedicated MEX trigger has its own hard checks"
        elif is_momentum_orb:
            # Momentum+ORB has its own independent 5m indicator set and explicit
            # breakout/retest confirmation. Do not re-run normal-strategy gates.
            orb_metrics = dict(c.get("momentum_orb_metrics") or {})
            orb_raw = abs(float(c["votes"][0].score)) * 100.0
            floor = float(cfg.MEX_ORB_SCORE_MIN)
            score_10 = round(min(10.0, 8.0 + 2.0 * max(0.0, orb_raw - floor) / max(1.0, 100.0 - floor)), 2)
            diagnostic_metrics = dict(orb_metrics)
            diagnostic_metrics["score_components"] = {"momentum_explosion_orb": round(orb_raw, 2)}
            gate = {"metrics": diagnostic_metrics}
            score_reasons = [
                str(c["votes"][0].reason),
                f"Dedicated Momentum+ORB quality {orb_raw:.1f}/100; live breakout acceptance passed (retest optional)",
                f"ADX {orb_metrics.get('adx', 0):.1f}; RSI {orb_metrics.get('rsi', 0):.1f}; "
                f"RVOL {orb_metrics.get('relative_volume', 0):.2f}x; range expansion {orb_metrics.get('range_expansion', 0):.2f}x",
                f"EMA9/21 {orb_metrics.get('ema9', 0):.2f}/{orb_metrics.get('ema21', 0):.2f}; VWAP {orb_metrics.get('vwap', 0):.2f}",
            ]
            score_rejects = []
            extra, extra_note = 0.0, "not applied: dedicated Momentum+ORB gates are authoritative"
        elif is_v2:
            v2_candidate = c["v2_candidate"]
            score_10 = round(float(v2_candidate.score) / 10.0, 2)
            diagnostic_metrics = dict(v2_candidate.metadata or {})
            diagnostic_metrics["score_components"] = {
                "unified_v2_score_100": round(float(v2_candidate.score), 2)
            }
            gate = {"metrics": diagnostic_metrics}
            score_reasons = list(v2_candidate.reasons) + [
                f"Unified balanced score {v2_candidate.score:.1f}/100 | grade {v2_candidate.metadata.get('grade', 'B')}",
                f"Independent strategy: {v2_candidate.strategy}",
                "Selected after same-candle direction conflict resolution",
            ]
            score_rejects = []
            extra, extra_note = 0.0, "not applied: unified v2 scorer is authoritative"
            if v2_candidate.score < float(cfg.V2_MIN_SCORE):
                return self._reject("v2 score gate", u.name,
                                    f"{v2_candidate.score:.1f}/100 < {cfg.V2_MIN_SCORE:.1f}/100")
        elif is_normal_trend:
            normal_metrics = dict(c.get("normal_metrics") or {})
            score_10 = 8.0
            diagnostic_metrics = dict(normal_metrics)
            diagnostic_metrics["score_components"] = {"normal_trend_indicators": 1.0}
            gate = {"metrics": diagnostic_metrics}
            score_reasons = [
                f"Independent Normal Trend {normal_metrics.get('setup_type', 'CONFIRMED_SETUP')}: EMA9/21, MACD(12/26/9), session VWAP, RSI(14), ADX(14) with directional DI",
                str(c["votes"][0].reason),
                f"ADX {normal_metrics.get('adx', 0):.1f}; +DI {normal_metrics.get('plus_di', 0):.1f}; -DI {normal_metrics.get('minus_di', 0):.1f}; RSI {normal_metrics.get('rsi', 0):.1f}; MACD histogram {normal_metrics.get('macd_histogram', 0):.4f}",
                f"15-minute context aligned: {'bullish' if htf == 1 else 'bearish'}",
            ]
            score_rejects = []
            extra, extra_note = 0.0, "not applied: normal trend engine is authoritative"
        else:
            gate = scorer.score_10_point(side_hint, c["session"], c["allc"], reg, htf=htf, mkt=mkt, votes=c["votes"], htf_vwap=htf_vwap)
            score_10 = float(gate["score"])
            score_reasons = list(gate["reasons"]) + [f"Regime: {reg.get('label', 'MIXED')}"]
            score_rejects = list(gate["rejects"])
            if score_rejects or score_10 < cfg.ALERT_SCORE_MIN:
                self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "NEAR_THRESHOLD", score_10, score_reasons + score_rejects, now)
                return self._reject("score gate", u.name, f"{score_10:.2f}/10 < {cfg.ALERT_SCORE_MIN:.1f}/10 or hard gates {score_rejects}")
            strategy_for_adapt = strategy_of(c["votes"][0].name) if c["votes"] else "TREND"
            extra, extra_note = self.adaptive.extra_score(strategy_for_adapt, u.kind, now)
            if extra > 0 and score_10 < cfg.ALERT_SCORE_MIN + extra:
                self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "NEAR_THRESHOLD", score_10,
                            score_reasons + [f"adaptive bar +{extra:.1f}: {extra_note}"], now)
                return self._reject("adaptive bar", u.name,
                                    f"{score_10:.2f}/10 < {cfg.ALERT_SCORE_MIN + extra:.2f}/10 ({extra_note})")
        side, conf = side_hint, score_10
        agree = independent_agree
        strategy = (c["v2_candidate"].strategy if is_v2 else
                    "SIMPLE_TREND" if is_simple else "NORMAL_TREND" if is_normal_trend
                    else strategy_of(c["votes"][0].name) if c["votes"] else "TREND")
        tier = ("EARLY" if str((entry_candidate or {}).get("mode", "")).upper() == "INTRABAR" else "CONFIRMED") if is_v2 else "CONFIRMED"
        reasons = list(score_reasons) + scorer.reasons(c["votes"], side)
        trend_note = "15m trend aligned" if htf == (1 if side == "CALL" else -1) else "15m trend mixed"
        vwap_note = "15m VWAP aligned" if htf_vwap == (1 if side == "CALL" else -1) else ("15m VWAP opposed (score-only)" if htf_vwap in (-1, 1) else "15m VWAP neutral")
        reasons.append(f"Trend: {trend_note}; {vwap_note}" + (f", ADX {reg['adx']:.0f}" if reg.get("adx") else ""))
        if reg["notes"]:
            reasons.append("Regime: " + ", ".join(n for n in reg["notes"] if not n.startswith("Trend strength")))

        # Retain OI-wall levels as informational alert metadata only; they never veto a signal.
        res, sup = chain.wall("CE"), chain.wall("PE")
        ivr = calc_iv_rank(chain.atm_iv(), self.db.iv_hist(u.name))
        if is_v2:
            strict_selection, why = select_validated_contract(
                self.master, u, side, chain, datetime.now(IST), iv_rank=ivr
            )
            contract = strict_selection.contract if strict_selection else None
            q = strict_selection.quote if strict_selection else None
        else:
            contract, q, why = pick_contract(self.master, u, side, chain, now, ivr)
        if not contract:
            self._watch(u.name, side, entry_bar.ts, c["ps"], "INVALIDATED", score_10, score_reasons + [f"contract rejected: {why}"], now)
            return self._reject("contract rejected", u.name, f"{side} ({why})")
        # A buy entry must be priced from the current ask. Never substitute LTP:
        # the last trade may be stale or below the price currently available to buy.
        if is_v2:
            quote_ok, quote_reason = validate_option_quote(q, u)
            if not quote_ok:
                self._watch(u.name, side, entry_bar.ts, c["ps"], "INVALIDATED",
                            score_10, score_reasons + [f"strict quote gate: {quote_reason}"], now)
                return self._reject("strict option quote gate", u.name, quote_reason)
        px = _executable_buy_price(q)
        if px is None:
            self._watch(u.name, side, entry_bar.ts, c["ps"], "INVALIDATED", score_10, score_reasons + ["no executable option price"], now)
            return self._reject("no executable price", u.name, side)
        # Normal/SIMPLE uses the same nearby structural invalidation reference as
        # the legacy trend path (EMA9, session VWAP, or trigger-candle extreme).
        # RiskRules still rejects stops outside its permitted ATR envelope.
        required_rr = (float(cfg.V2_MIN_RR) if is_v2 else
                        max(float(cfg.MIN_RR_T1), float(getattr(cfg, "SIMPLE_MIN_RR_T1", 1.5)))
                        if is_simple else float(cfg.MIN_RR_T1))
        stop_dist = (abs(spot - float(c["v2_candidate"].stop)) if is_v2 else
                     invalidation_distance(side, c["session"], c["allc"], spot, strategy,
                                           trigger_bar=entry_bar if is_simple else None))
        lv = self.risk.levels(px, q.delta, a5, contract.tick_size, side, spot, stop_dist,
                              min_target_rr=required_rr if (is_simple or is_v2) else None)
        if not lv or float(lv.get("rr", 0.0)) < float(cfg.MIN_RR_T1):
            self._watch(u.name, side, entry_bar.ts, c["ps"], "INVALIDATED", score_10, score_reasons + ["risk/reward validation failed"], now)
            return self._reject("risk levels rejected", u.name, side)
        if is_simple:
            underlying_risk = abs(spot - float(lv.get("und_sl") or 0.0))
            underlying_reward = (float(lv.get("und_t1") or 0.0) - spot) * (1 if side == "CALL" else -1)
            first_target_rr = underlying_reward / underlying_risk if underlying_risk > 0 else 0.0
            if underlying_reward <= 0 or first_target_rr + 1e-9 < required_rr:
                detail = f"Normal first-target RR {first_target_rr:.2f} below required {required_rr:.2f}"
                self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "INVALIDATED", score_10, score_reasons + [detail], now)
                return self._reject("normal minimum reward/risk", u.name, detail)
        # A repeated swing level immediately ahead of price can cap the realistic
        # first target. Reduce T1 to just before that obstacle, or reject only
        # when the remaining structural reward no longer meets the existing RR.
        if is_simple or (cfg.V3_PROFILE_ENABLED and (is_normal_trend or is_mex or is_momentum_orb)):
            obstacle = nearest_structure_obstacle(side, c["allc"], spot)
            if obstacle is not None:
                obstacle_target = float(obstacle) - sgn * (0.10 * a5)
                current_target = float(lv.get("und_t1") or 0.0)
                before_target = (spot < obstacle < current_target) if sgn > 0 else (current_target < obstacle < spot)
                if before_target:
                    structural_risk = (spot - float(lv["und_sl"])) * sgn
                    structural_reward = (obstacle_target - spot) * sgn
                    obstacle_rr = structural_reward / structural_risk if structural_risk > 0 else 0.0
                    if structural_reward <= 0 or obstacle_rr < required_rr:
                        detail = (f"nearby {'resistance' if sgn > 0 else 'support'} at {obstacle:.2f} "
                                  f"leaves RR {obstacle_rr:.2f} below {required_rr:.2f}")
                        self._watch(u.name, side, entry_bar.ts, c["ps"], "INVALIDATED",
                                    score_10, score_reasons + [detail], now)
                        return self._reject("nearby structure blocks target", u.name, detail)
                    lv["und_t1"] = obstacle_target
                    tick = max(0.01, float(contract.tick_size or 0.05))
                    premium_target = px + abs(float(q.delta or 0.5)) * sgn * (obstacle_target - spot)
                    lv["t1"] = math.ceil(premium_target / tick - 1e-9) * tick
                    score_reasons.append(
                        f"first target capped before nearby {'resistance' if sgn > 0 else 'support'} {obstacle:.2f}"
                    )
        valid_structure, structure_reason, structure_metrics = validate_entry_candidate(
            entry_candidate, entry_bar, spot, a5,
            cfg.LIVE_MAX_ADVERSE_ATR, cfg.LIVE_MAX_RUNAWAY_ATR,
            structural_stop=lv.get("und_sl"), target=lv.get("und_t1"), min_rr=required_rr)
        if not valid_structure:
            self._watch(u.name, side_hint, entry_bar.ts, c["ps"], "INVALIDATED",
                        score_10, score_reasons + [structure_reason], now)
            return self._reject(structure_reason, u.name, str(structure_metrics))
        valid_till, exit_by = self.risk.validity(now)
        lot = contract.lot_size or u.lot_size
        max_loss_per_lot = max(0.0, (px - lv["sl"]) * lot)      # informational only: nothing is rejected on it
        signal_candle_ts = entry_bar.ts
        intrabar_entry = str((entry_candidate or {}).get("mode", "")).upper() == "INTRABAR"
        candle_identity = signal_candle_ts.strftime("%Y%m%dT%H%M%S")
        preview_key = (u.name, signal_candle_ts.isoformat())
        if is_v2 and not intrabar_entry:
            with self._v2_preview_lock:
                earlier_preview = self._v2_early_previews.pop(preview_key, None)
            if earlier_preview:
                if earlier_preview.get("side") == side:
                    score_reasons.append("Earlier provisional intrabar preview was confirmed by this closed candle")
                else:
                    score_reasons.append(
                        f"Closed candle reversed earlier provisional {earlier_preview.get('side')} preview; "
                        "closed-bar direction takes priority"
                    )
        signal_timestamp = (datetime.fromisoformat(entry_candidate["confirmation_time"])
                            if intrabar_entry else signal_candle_ts + timedelta(minutes=cfg.SCAN_MINUTES))
        sig = Signal(
            # Stable across repeated scans and process restarts: identity is tied
            # to the completed setup candle, never to scan/delivery wall-clock time.
            id=f"{u.name}-{candle_identity}-{side[0]}", ts=signal_timestamp.isoformat(timespec="seconds"), underlying=u.name, side=side,
            contract=contract.trading_symbol, instrument_key=contract.instrument_key, expiry=expiry.isoformat(),
            strike=contract.strike, opt_type=contract.opt_type, lot_size=lot, ltp=px,
            entry_lo=lv["entry_lo"], entry_hi=lv["entry_hi"], sl=lv["sl"], t1=lv["t1"], t2=lv["t2"], t3=lv.get("t3", 0.0), spot=spot,
            fut_price=fut[-1].c if fut else 0.0, und_sl=lv["und_sl"], und_t1=lv["und_t1"], und_t2=lv["und_t2"], und_t3=lv.get("und_t3", 0.0),
            valid_till=valid_till, exit_by=exit_by, cost_lot=px * lot, max_loss_lot=max_loss_per_lot, rr=lv["rr"],
            delta=q.delta, theta=q.theta, iv=q.iv, iv_rank=ivr, spread_pct=q.spread_pct, oi=q.oi, volume=q.volume,
            pcr=chain.pcr, support=sup[0] if sup else 0, resistance=res[0] if res else 0, dte=(expiry - today).days,
            confidence=conf, agree=agree, reasons=reasons, strategy=strategy, tier=tier,
            kind=u.kind, cur_sl=lv["sl"], prem_high=px, provider=self.fo.active_name,
            status="EARLY" if (is_v2 and intrabar_entry) else "ACTIVE")
        self._watch(u.name, side, entry_bar.ts, c["ps"],
                    "EARLY_PREVIEW" if (is_v2 and intrabar_entry) else "CONFIRMED",
                    score_10, score_reasons, now)
        dedupe_key = f"alert:{signal_candle_ts.date().isoformat()}:{u.name}:{candle_identity}:{side}"
        if is_v2:
            dedupe_key += ":EARLY" if intrabar_entry else ":CONFIRMED"
        if self._already_alerted(dedupe_key):
            return self._reject("duplicate alert suppressed", u.name, sig.id)
        preview_only = bool(is_v2 and intrabar_entry)
        if not preview_only:
            self.db.save_signal(sig)
        if not preview_only:
            try:      # features for tools/calibrate.py: what the signal looked like when it fired
                feats = dict(gate.get("metrics", {}))
                if is_v7_sequential:
                    feats["v7_sequential"] = v7_decision.metrics
                feats.update(cfeat)
                feats.update({"score": score_10, "strategy": strategy, "kind": u.kind, "side": side,
                              "votes": [getattr(v, "name", "") for v in c["votes"]], "adaptive_extra": extra,
                              "spread_pct": q.spread_pct, "dte": (expiry - today).days, "regime_label": reg.get("label"),
                              "entry_contract": entry_candidate, "entry_validation": structure_metrics})
                self.db.save_features(sig.id, sig.ts[:10], feats)
            except Exception:
                log.exception("signal feature logging failed")
        if not preview_only:
            self.tracker.add(sig, contract)
        telegram_acknowledged = self._queue_signal_alert(sig, dedupe_key, format_signal(sig), now)
        log.info("signal lifecycle | signal_id=%s | database_saved=%s | preview_only=%s | telegram_acknowledged=%s",
                 sig.id, not preview_only, preview_only, bool(telegram_acknowledged))
        if preview_only and not telegram_acknowledged:
            return None
        if preview_only:
            with self._v2_preview_lock:
                self._v2_early_previews[preview_key] = {
                    "side": side, "score": float(c["v2_candidate"].score),
                    "candle_ts": signal_candle_ts.isoformat(),
                }
        log.info("SIGNAL %s %s %s %s score=%.2f/10", sig.underlying, sig.side, sig.contract, strategy, conf)
        return sig


    def shutdown(self):
        """Stop accepting live work and cancel queued scanner tasks during process shutdown."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        outbox_thread = getattr(self, "_outbox_thread", None)
        candidate_thread = getattr(self, "_candidate_persist_thread", None)
        for thread in (outbox_thread, candidate_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.5)
        if not any(thread is not None and thread.is_alive() for thread in (outbox_thread, candidate_thread)):
            maintenance_db = getattr(self, "_outbox_db", None)
            if maintenance_db is not None:
                try:
                    maintenance_db.close()
                except Exception:
                    log.debug("maintenance DB close failed", exc_info=True)
        for pool_name in ("live_pool", "_live_chain_pool", "pool"):
            pool = getattr(self, pool_name, None)
            if pool is not None:
                try:
                    # Do not block process shutdown on a provider request that
                    # has exceeded its own timeout. Queued work is cancelled;
                    # running workers finish under the provider's request timeout.
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
                except RuntimeError:
                    pass
