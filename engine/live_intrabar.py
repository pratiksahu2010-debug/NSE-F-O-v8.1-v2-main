"""Live intrabar state and early-trigger detection.

Partial candles are kept separate from the completed-candle history. Upstox's
volume field is treated as a cumulative session counter: only positive deltas
are added to the current partial candle. A reconnect/rebase never invents
volume. This module only proposes candidates; the scanner still applies every
hard entry, option-liquidity and risk check before alerting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from statistics import median
from typing import Dict, Optional

from indicators.core import adx, atr, ema, htf_bias, macd, rsi, vwap
from models import Candle, Underlying
from strategies.normal_trend import evaluate_normal_trend_intrabar
from strategies.simple_signal import evaluate_simple_intrabar
from utils.market_hours import IST


@dataclass
class LiveBar:
    start: datetime
    o: float
    h: float
    l: float
    c: float
    volume: float = 0.0
    ticks: int = 0
    beyond_count: int = 0
    last_cumulative_volume: Optional[float] = None
    last_tick_at: Optional[datetime] = None

    def candle(self) -> Candle:
        return Candle(self.start, self.o, self.h, self.l, self.c, self.volume, 0.0)


class LiveIntrabarEngine:
    """Thread-safe 5m partial-bar aggregator and three independent early triggers."""
    def __init__(self, scan_minutes: int = 5, stale_seconds: float = 8.0):
        self.scan_minutes = max(1, int(scan_minutes))
        self.stale_seconds = max(1.0, float(stale_seconds))
        self._lock = RLock()
        self._bars: Dict[str, LiveBar] = {}
        self._last_tick: Dict[str, datetime] = {}
        self._last_candidate: Dict[str, tuple] = {}
        self._sent_candidates = set()
        self._normal_stats = {"evaluated": 0, "detected": 0, "no_trigger": 0,
                              "priority_superseded": 0, "setups": {}, "gate_failures": {}, "latest": {}}

    def begin_cycle(self):
        with self._lock:
            self._normal_stats = {"evaluated": 0, "detected": 0, "no_trigger": 0,
                                  "priority_superseded": 0, "setups": {}, "gate_failures": {}, "latest": {}}

    def normal_snapshot(self):
        with self._lock:
            return {**self._normal_stats,
                    "setups": dict(self._normal_stats["setups"]),
                    "gate_failures": dict(self._normal_stats["gate_failures"]),
                    "latest": dict(self._normal_stats["latest"])}

    def _bucket(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        else:
            now = now.astimezone(IST)
        minute = now.minute - now.minute % self.scan_minutes
        return now.replace(minute=minute, second=0, microsecond=0)

    def update(self, symbol: str, ltp: float, cumulative_volume: float,
               now: datetime) -> Optional[Candle]:
        try:
            price = float(ltp)
            vol = float(cumulative_volume or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        bucket = self._bucket(now)
        with self._lock:
            bar = self._bars.get(symbol)
            if bar is None or bar.start != bucket:
                # Start a new partial candle. Its first volume delta is unknown,
                # so establish a baseline and wait for the next feed update.
                bar = LiveBar(bucket, price, price, price, price,
                              last_cumulative_volume=vol if vol > 0 else None,
                              last_tick_at=now)
                self._bars[symbol] = bar
            else:
                if bar.last_cumulative_volume is not None and vol >= bar.last_cumulative_volume:
                    bar.volume += max(0.0, vol - bar.last_cumulative_volume)
                # A falling counter means a session reset/reconnect: rebase it.
                bar.last_cumulative_volume = vol if vol > 0 else bar.last_cumulative_volume
                bar.h = max(bar.h, price)
                bar.l = min(bar.l, price)
                bar.c = price
                bar.ticks += 1
                bar.last_tick_at = now
            self._last_tick[symbol] = now
            return bar.candle()

    def snapshot(self, symbol: str, now: datetime) -> Optional[Candle]:
        with self._lock:
            bar = self._bars.get(symbol)
            ts = self._last_tick.get(symbol)
            if bar is None or ts is None:
                return None
            age = (now - ts).total_seconds()
            if age < -2 or age > self.stale_seconds:
                return None
            return bar.candle()

    def mark_sent(self, setup_id: str):
        with self._lock:
            self._sent_candidates.add(str(setup_id))

    def detect(self, und: Underlying, completed_session, completed_all,
               partial: Candle, now: datetime, cfg, allow_sequential: bool = True,
               allow_mex: bool = True, allow_orb: bool = True,
               allow_simple: bool = False) -> Optional[dict]:
        """Return one early trigger contract, or None. No partial bar enters closed history."""
        # SIMPLE needs SIMPLE_MIN_BARS (35 by default), not the legacy V7
        # history floor (50). The old shared check delayed valid SIMPLE live
        # entries by three 5m candles despite sufficient indicator history.
        if allow_sequential:
            required_history = max(30, int(getattr(cfg, "V7_MIN_BARS", 50)))
        elif allow_simple:
            required_history = max(30, int(getattr(cfg, "SIMPLE_MIN_BARS", 35)))
        else:
            required_history = 35  # MEX/ORB indicator warm-up
        if len(completed_all) < required_history:
            return None
        if not completed_session or partial.ts.date() != now.date():
            return None
        closes = [float(x.c) for x in completed_all]
        e9, e21 = ema(closes, 9), ema(closes, 21)
        av = atr(completed_all, 14)
        a = av[-1] if av else None
        if not a or a <= 0:
            return None
        # VWAP is calculated from completed session bars only; partial price
        # is compared to it, avoiding a false "closed candle" calculation.
        vw_values = vwap(completed_session)
        vw = vw_values[-1] if vw_values else None
        if not vw or vw <= 0:
            return None
        adx_rows = adx(completed_all, 14)
        rsi_rows = rsi(closes, 14)
        if not adx_rows or not rsi_rows or adx_rows[-1] is None or rsi_rows[-1] is None:
            return None
        adx_value, plus_di, minus_di = adx_rows[-1]
        rsi_value = float(rsi_rows[-1])
        if adx_value is None or plus_di is None or minus_di is None:
            return None
        min_adx = max(18.0, float(getattr(cfg, "SETUP_MIN_ADX", 20.0)))
        di_gap = float(getattr(cfg, "DI_MIN_GAP", 2.0))
        vol_baseline = [x.v for x in completed_all[-20:] if x.v > 0]
        avg_volume = sum(vol_baseline) / len(vol_baseline) if vol_baseline else 0.0
        live_rvol = partial.v / avg_volume if avg_volume > 0 else 0.0

        # Live bar strength and extension.
        rng = max(partial.h - partial.l, 1e-9)
        body = abs(partial.c - partial.o) / rng
        close_pos = (partial.c - partial.l) / rng
        if partial.c <= 0:
            return None

        # Opening-range levels are built exclusively from completed 09:15 onward bars.
        opening = [x for x in completed_session
                   if (x.ts.hour, x.ts.minute) >= (9, 15)
                   and (x.ts.hour, x.ts.minute) < (9, 30)]
        orb_hi = max((x.h for x in opening), default=0.0)
        orb_lo = min((x.l for x in opening), default=0.0)

        side = None
        strategy = None
        reason = ""
        edge = 0.0

        trigger_metrics = {}

        # ORB: opening-range breakout is evaluated on the live partial candle.
        # A failed earlier break may re-arm only after a completed close returns
        # inside the range; a prior breakout does not permanently consume ORB.
        orb_time_ok = (now.hour, now.minute) <= tuple(getattr(cfg, "MEX_ORB_LAST_TIME", (14, 45)))
        orb_bars = max(1, int(getattr(cfg, "ORB_BARS", 3)))
        orb_ready = (
            len(opening) == orb_bars
            and (opening[0].ts.hour, opening[0].ts.minute) == (9, 15)
            and all((right.ts - left.ts).total_seconds() == 300
                    for left, right in zip(opening, opening[1:]))
        )
        if allow_orb and orb_time_ok and getattr(cfg, "MOMENTUM_ORB_ENABLED", True) and orb_ready:
            orb_width = orb_hi - orb_lo
            range_ok = float(getattr(cfg, "ORB_MIN_RANGE_ATR", 0.6)) * a <= orb_width <= float(getattr(cfg, "ORB_MAX_RANGE_ATR", 3.5)) * a
            after_open = [x for x in completed_session if x.ts > opening[-1].ts]
            previous_close = float(after_open[-1].c) if after_open else float(opening[-1].c)
            body_min = float(getattr(cfg, "MEX_ORB_BODY_MIN", 0.50))
            close_threshold = float(getattr(cfg, "MEX_ORB_CLOSE_POS", 0.65))
            clearance_min = float(getattr(cfg, "MEX_ORB_CLEARANCE_ATR", 0.05))
            extension_max = float(getattr(cfg, "MEX_ORB_MAX_EXTENSION_ATR", 1.5))
            call_clearance = (partial.c - orb_hi) / a
            put_clearance = (orb_lo - partial.c) / a
            call_extension = (partial.c - e9[-1]) / a
            put_extension = (e9[-1] - partial.c) / a
            call_fresh = previous_close <= orb_hi
            put_fresh = previous_close >= orb_lo
            call_ok = (call_fresh and call_clearance >= clearance_min
                       and partial.c > partial.o and body >= body_min
                       and close_pos >= close_threshold and call_extension <= extension_max)
            put_ok = (put_fresh and put_clearance >= clearance_min
                      and partial.c < partial.o and body >= body_min
                      and close_pos <= (1.0 - close_threshold) and put_extension <= extension_max)
            if range_ok and call_ok:
                side, strategy, edge = "CALL", "MOMENTUM_ORB", orb_hi
                reason = "live ORB LONG: opening-range break with strong body/close and controlled extension"
                trigger_metrics = {"orb_high": orb_hi, "orb_low": orb_lo,
                                   "orb_midpoint": (orb_hi + orb_lo) / 2.0, "orb_width": orb_width,
                                   "breakout_clearance_atr": call_clearance, "extension_atr": call_extension,
                                   "body_fraction": body, "close_position": close_pos}
            elif range_ok and put_ok:
                side, strategy, edge = "PUT", "MOMENTUM_ORB", orb_lo
                reason = "live ORB SHORT: opening-range breakdown with strong body/close and controlled extension"
                trigger_metrics = {"orb_high": orb_hi, "orb_low": orb_lo,
                                   "orb_midpoint": (orb_hi + orb_lo) / 2.0, "orb_width": orb_width,
                                   "breakout_clearance_atr": put_clearance, "extension_atr": put_extension,
                                   "body_fraction": body, "close_position": close_pos}

        # MEX: live range expansion + directional impulse + fresh boundary break.
        # MACD, RSI, VWAP, ADX and volume are diagnostics/supporting context only;
        # none can veto an otherwise valid independent momentum expansion.
        if side is None and allow_mex and getattr(cfg, "MEX_ENABLED", True) and len(completed_all) >= 35:
            lookback = max(5, int(getattr(cfg, "MEX_LOOKBACK", 8)))
            base = completed_all[-lookback:]
            base_ranges = [max(0.0, x.h - x.l) for x in base]
            base_range = median(base_ranges) if base_ranges else 0.0
            broad_ranges = [max(0.0, x.h - x.l) for x in completed_all[-30:]]
            broad_median = median(broad_ranges) if broad_ranges else base_range
            compression = median(base_ranges) / max(broad_median, 1e-9) if base_ranges else 1.0
            compressed = compression <= float(getattr(cfg, "MEX_COMPRESSION_MAX", 0.90))
            range_exp = rng / max(base_range, 1e-9)
            min_range_ratio = float(getattr(cfg, "MEX_MIN_RANGE_RATIO", 1.5))
            min_impulse = float(getattr(cfg, "MEX_MIN_IMPULSE_ATR", 0.45))
            close_threshold = float(getattr(cfg, "MEX_CLOSE_POS", 0.75))
            extension_max = float(getattr(cfg, "MEX_MAX_EXTENSION_ATR", 1.8))
            _, _, hist = macd(closes, 12, 26, 9)
            macd_call_support = hist[-1] > 0 and hist[-1] >= hist[-2]
            macd_put_support = hist[-1] < 0 and hist[-1] <= hist[-2]
            impulse_call = (partial.c - partial.o) / a
            impulse_put = (partial.o - partial.c) / a
            call_extension = (partial.c - e9[-1]) / a
            put_extension = (e9[-1] - partial.c) / a
            call_boundary = max(x.h for x in base)
            put_boundary = min(x.l for x in base)
            call_break = partial.c > call_boundary
            put_break = partial.c < put_boundary
            expansion_ok = range_exp >= min_range_ratio
            live_adx, live_plus_di, live_minus_di = adx(completed_all + [partial], 14)[-1]
            adx_ok = float(live_adx) >= 18.0
            if (adx_ok and expansion_ok and impulse_call >= min_impulse and call_break
                    and close_pos >= close_threshold and call_extension <= extension_max):
                side, strategy, edge = "CALL", "MOMENTUM_EXPLOSION", call_boundary
                reason = "live MEX LONG: range expansion, ATR impulse and fresh boundary break"
                trigger_metrics = {"range_expansion": range_exp, "compression_ratio": compression,
                                   "compressed_before_release": compressed, "impulse_atr": impulse_call,
                                   "extension_atr": call_extension, "breakout_level": call_boundary,
                                   "close_position": close_pos, "macd_supporting": macd_call_support,
                                   "relative_volume": round(float(live_rvol), 3), "adx": float(live_adx),
                                   "plus_di": float(live_plus_di), "minus_di": float(live_minus_di)}
            elif (adx_ok and expansion_ok and impulse_put >= min_impulse and put_break
                    and close_pos <= (1.0 - close_threshold) and put_extension <= extension_max):
                side, strategy, edge = "PUT", "MOMENTUM_EXPLOSION", put_boundary
                reason = "live MEX SHORT: range expansion, ATR impulse and fresh boundary break"
                trigger_metrics = {"range_expansion": range_exp, "compression_ratio": compression,
                                   "compressed_before_release": compressed, "impulse_atr": impulse_put,
                                   "extension_atr": put_extension, "breakout_level": put_boundary,
                                   "close_position": close_pos, "macd_supporting": macd_put_support,
                                   "relative_volume": round(float(live_rvol), 3), "adx": float(live_adx),
                                   "plus_di": float(live_plus_di), "minus_di": float(live_minus_di)}

        # Normal Trend shares the same setup paths and indicator gates with the
        # closed-candle detector; only the trigger candle is partial.
        normal_result = None
        if allow_sequential:
            normal_result = evaluate_normal_trend_intrabar(
                completed_session, completed_all, partial,
                adx_min=25.0 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_ADX_MIN", 20.0)),
                adx_established=25.0 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_ADX_ESTABLISHED", 25.0)),
                di_min_gap=0.0 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_DI_MIN_GAP", 3.0)),
                rsi_long_min=50.0 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_RSI_LONG_MIN", 48.0)),
                rsi_short_max=50.0 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_RSI_SHORT_MAX", 52.0)),
                candle_body_min=0.25 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_CANDLE_BODY_MIN", 0.35)),
                close_pos_min=0.55 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_CLOSE_POS_MIN", 0.62)),
                pullback_atr=float(getattr(cfg, "NORMAL_PULLBACK_ATR", 0.20)),
                max_entry_extension_atr=1.50 if cfg.V3_PROFILE_ENABLED else float(getattr(cfg, "NORMAL_MAX_EXTENSION_ATR", 0.60)),
                breakout_lookback=int(getattr(cfg, "NORMAL_BREAKOUT_LOOKBACK", 6)),
                rvol_min=1.2 if cfg.V3_PROFILE_ENABLED else 0.0,
            )
            with self._lock:
                stats = self._normal_stats
                stats["evaluated"] += 1
                if normal_result.get("side") in (1, -1):
                    stats["detected"] += 1
                    setup_name = str(normal_result.get("setup_type") or "UNKNOWN")
                    stats["setups"][setup_name] = stats["setups"].get(setup_name, 0) + 1
                else:
                    stats["no_trigger"] += 1
                    for gate in normal_result.get("gate_failures", []):
                        stats["gate_failures"][gate] = stats["gate_failures"].get(gate, 0) + 1
                stats["latest"][und.name] = {
                    "reason": normal_result.get("reason", ""),
                    "setup_type": normal_result.get("setup_type"),
                    "gate_failures": list(normal_result.get("gate_failures", [])),
                    "metrics": dict(normal_result.get("metrics") or {}),
                }
                if len(stats["latest"]) > 100:
                    stats["latest"].pop(next(iter(stats["latest"])))
            normal_context = htf_bias(completed_all, cfg.HTF_MINUTES) if cfg.V3_PROFILE_ENABLED else 0
            normal_context_ok = (
                not cfg.V3_PROFILE_ENABLED
                or (normal_result.get("side") == 1 and normal_context == 1)
                or (normal_result.get("side") == -1 and normal_context == -1)
            )
            if normal_result.get("side") in (1, -1) and side is None and normal_context_ok:
                side = "CALL" if normal_result["side"] > 0 else "PUT"
                strategy = "NORMAL_TREND"
                edge = float((normal_result.get("metrics") or {}).get("trigger_edge") or e9[-1])
                reason = str(normal_result.get("reason") or "Normal Trend intrabar confirmation")
                if cfg.V3_PROFILE_ENABLED:
                    reason += " | completed 15m context aligned"
            elif normal_result.get("side") in (1, -1) and side is None and not normal_context_ok:
                with self._lock:
                    self._normal_stats["gate_failures"]["15m_context_directional_mismatch"] = (
                        self._normal_stats["gate_failures"].get("15m_context_directional_mismatch", 0) + 1
                    )
            elif normal_result.get("side") in (1, -1) and side is not None:
                with self._lock:
                    self._normal_stats["priority_superseded"] += 1

        simple_result = None
        if side is None and allow_simple:
            simple_result = evaluate_simple_intrabar(
                completed_session, completed_all, partial,
                adx_min=float(getattr(cfg, "SIMPLE_ADX_MIN", 25.0)),
                rsi_call_min=float(getattr(cfg, "SIMPLE_RSI_CALL_MIN", 55.0)),
                rsi_call_max=float(getattr(cfg, "SIMPLE_RSI_CALL_MAX", 75.0)),
                rsi_put_max=float(getattr(cfg, "SIMPLE_RSI_PUT_MAX", 45.0)),
                rsi_put_min=float(getattr(cfg, "SIMPLE_RSI_PUT_MIN", 25.0)),
                fresh_bars=int(getattr(cfg, "SIMPLE_FRESH_BARS", 3)),
                min_bars=int(getattr(cfg, "SIMPLE_MIN_BARS", 35)),
                min_session_bars=int(getattr(cfg, "SIMPLE_MIN_SESSION_BARS", 3)),
                max_entry_extension_atr=float(getattr(cfg, "SIMPLE_MAX_EXTENSION_ATR", 1.5)),
                context_15m=htf_bias(completed_all, cfg.HTF_MINUTES),
            )
            # Surface SIMPLE live-trigger activity in the same diagnostics
            # snapshot as the legacy Normal detector. Otherwise live SIMPLE
            # could be evaluating every tick while its diagnostic counters stay zero.
            with self._lock:
                stats = self._normal_stats
                stats["evaluated"] += 1
                if simple_result.get("side") in (1, -1):
                    stats["detected"] += 1
                    setup_name = str(simple_result.get("setup_type") or "SIMPLE_TREND")
                    stats["setups"][setup_name] = stats["setups"].get(setup_name, 0) + 1
                else:
                    stats["no_trigger"] += 1
                    for gate in simple_result.get("gate_failures", []):
                        stats["gate_failures"][gate] = stats["gate_failures"].get(gate, 0) + 1
                stats["latest"][und.name] = {
                    "reason": simple_result.get("reason", ""),
                    "setup_type": simple_result.get("setup_type") or "SIMPLE_TREND",
                    "gate_failures": list(simple_result.get("gate_failures", [])),
                    "metrics": dict(simple_result.get("metrics") or {}),
                }
                if len(stats["latest"]) > 100:
                    stats["latest"].pop(next(iter(stats["latest"])))
            if simple_result.get("side") in (1, -1):
                side = "CALL" if simple_result["side"] > 0 else "PUT"
                strategy = "SIMPLE_TREND"
                edge = float(partial.c)
                reason = str(simple_result.get("reason") or "early SIMPLE Normal trigger")

        if side is None:
            return None
        # Do not chase an already extended live print.
        displacement = (partial.c - edge) / a if side == "CALL" else (edge - partial.c) / a
        if displacement < 0 or displacement > float(getattr(cfg, "LIVE_MAX_RUNAWAY_ATR", 0.60)):
            return None
        key = f"{und.name}:{strategy}:{side}:{partial.ts.isoformat()}"
        with self._lock:
            previous = self._last_candidate.get(und.name)
            if key in self._sent_candidates:
                return None
            if previous and previous[0] == key and (now - previous[1]).total_seconds() < 3.0:
                return None
            self._last_candidate[und.name] = (key, now)
        return {
            "symbol": und.name, "strategy": strategy, "side": side,
            "setup_time": completed_all[-1].ts.isoformat(),
            "trigger_time": partial.ts.isoformat(),
            "confirmation_time": now.isoformat(),
            "confirmation_price": float(partial.c),
            "trigger_edge": float(edge), "atr": float(a),
            "timeframe": "5m-intrabar", "setup_id": key,
            "reason": reason, "bar": partial,
            "metrics": ({**(simple_result.get("metrics") or {}), "normal_setup_type": simple_result.get("setup_type")}
                        if strategy == "SIMPLE_TREND" and simple_result else
                        {**(normal_result.get("metrics") or {}), "normal_setup_type": normal_result.get("setup_type")}
                        if strategy == "NORMAL_TREND" and normal_result else
                        {**trigger_metrics, "adx": float(adx_value), "plus_di": float(plus_di),
                         "minus_di": float(minus_di), "rsi": rsi_value,
                         "relative_volume": round(float(live_rvol), 3),
                         "ema9": float(e9[-1]), "ema21": float(e21[-1]),
                         "vwap": float(vw), "atr": float(a)}),
        }
