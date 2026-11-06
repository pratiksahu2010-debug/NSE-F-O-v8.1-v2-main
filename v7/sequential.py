"""V7.1 completed-candle sequential confirmation with full per-symbol diagnostics."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Sequence

from models import Candle
from .indicators import ema, rsi, adx, atr, session_vwap, session_vwap_mode


@dataclass
class Decision:
    status: str
    direction: Optional[str]
    reason: str
    metrics: dict


class SequentialEngine:
    def __init__(self, crossover_max_bars=3, adx_min=25, vwap_max_atr=1.5,
                 rsi_call_max=75, rsi_put_min=25, min_history_bars=50, pullback_trend_bars=6, pullback_touch_atr=0.15,
                 max_ema_crosses=2, min_ema_sep_atr=0.10, max_vwap_crosses=2):
        self.crossover_max_bars = crossover_max_bars
        self.adx_min = adx_min
        self.vwap_max_atr = vwap_max_atr
        self.rsi_call_max = rsi_call_max
        self.rsi_put_min = rsi_put_min
        self.min_history_bars = max(50, int(min_history_bars))
        self.pullback_trend_bars = max(4, int(pullback_trend_bars))
        self.pullback_touch_atr = max(0.0, float(pullback_touch_atr))
        self.max_ema_crosses = max(0, int(max_ema_crosses))
        self.min_ema_sep_atr = max(0.0, float(min_ema_sep_atr))
        self.max_vwap_crosses = max(0, int(max_vwap_crosses))

    def evaluate(self, candles: Sequence[Candle], now=None, context_15m="UNAVAILABLE") -> Decision:
        # Provider timestamps are candle-open timestamps; a 5m bar is final only after open + 5m.
        bars = list(candles or [])
        if now is not None:
            bars = [c for c in bars if c.ts + timedelta(minutes=5) <= now]

        by_ts = {}
        for c in bars:
            try:
                if (c.c > 0 and c.h >= max(c.o, c.c, c.l)
                        and c.l <= min(c.o, c.c, c.h) and c.v >= 0):
                    by_ts[c.ts] = c
            except (TypeError, ValueError, AttributeError):
                continue
        bars = [by_ts[k] for k in sorted(by_ts)]
        if len(bars) < self.min_history_bars:
            return Decision("WAITING", None, f"insufficient completed candles (need {self.min_history_bars})", {
                "bars": len(bars), "required_bars": self.min_history_bars,
                "last_candle_open": bars[-1].ts.isoformat() if bars else None,
                "context_15m": context_15m,
            })

        close = [float(c.c) for c in bars]
        e9, e21 = ema(close, 9), ema(close, 21)
        vw = session_vwap(bars)
        vw_modes = session_vwap_mode(bars)
        rs = rsi(close, 14)
        ax = adx(bars, 14, 14)
        av = atr(bars, 14)
        i = len(bars) - 1
        c = bars[i]
        adx_tuple = ax[i]
        ad_value, pdi, ndi = adx_tuple if adx_tuple is not None else (None, None, None)
        metrics = {
            "bars": len(bars),
            "last_candle_open": c.ts.isoformat(),
            "last_candle_close": (c.ts + timedelta(minutes=5)).isoformat(),
            "ema9": e9[i], "ema21": e21[i],
            "ema_spread": e9[i] - e21[i],
            "vwap": vw[i], "vwap_mode": vw_modes[i],
            "rsi": rs[i], "adx": ad_value, "plus_di": pdi, "minus_di": ndi,
            "atr": av[i],
            "context_15m": context_15m,
        }

        # Regime quality: frequent EMA flips and VWAP recrosses identify rotational
        # sessions where otherwise-valid momentum entries are commonly whipsawed.
        recent_start = max(1, i - 19)
        ema_crosses = sum(
            1 for j in range(recent_start, i + 1)
            if (e9[j - 1] - e21[j - 1]) * (e9[j] - e21[j]) <= 0
            and (e9[j - 1] != e21[j - 1] or e9[j] != e21[j])
        )
        vwap_crosses = sum(
            1 for j in range(max(1, i - 11), i + 1)
            if vw[j - 1] is not None and vw[j] is not None
            and (bars[j - 1].c - vw[j - 1]) * (bars[j].c - vw[j]) < 0
        )
        ema_sep_atr = abs(e9[i] - e21[i]) / max(float(av[i] or 0.0), 1e-12)
        metrics.update({"ema_crosses_20": ema_crosses, "vwap_crosses_12": vwap_crosses,
                        "ema_separation_atr": round(ema_sep_atr, 4),
                        "market_regime": "CHOP" if ema_crosses > self.max_ema_crosses or vwap_crosses > self.max_vwap_crosses else "TREND_OR_TRANSITION"})

        # Older missing bars do not invalidate the indicator history. The latest five bars
        # must be contiguous because a fresh cross may be zero to three bars old.
        recent = bars[-5:]
        for prev, cur in zip(recent, recent[1:]):
            if prev.ts.date() == cur.ts.date() and (cur.ts - prev.ts).total_seconds() != 300:
                return Decision("WAITING", None, "recent 5m candle gap; waiting for contiguous confirmation bars", metrics)

        missing = [name for name, value in (
            ("VWAP", vw[i]), ("RSI", rs[i]), ("ADX/+DI/-DI", adx_tuple), ("ATR", av[i])
        ) if value is None]
        if missing:
            return Decision("WAITING", None, "indicator warmup/unavailable: " + ", ".join(missing), metrics)

        cross_up = [j for j in range(1, len(bars))
                    if e9[j - 1] <= e21[j - 1] and e9[j] > e21[j]]
        cross_down = [j for j in range(1, len(bars))
                      if e9[j - 1] >= e21[j - 1] and e9[j] < e21[j]]
        direction = None
        crossed = None
        if e9[i] > e21[i] and cross_up and i - cross_up[-1] <= self.crossover_max_bars:
            direction, crossed = "CALL", cross_up[-1]
        elif e9[i] < e21[i] and cross_down and i - cross_down[-1] <= self.crossover_max_bars:
            direction, crossed = "PUT", cross_down[-1]
        setup_type = "FRESH_EMA_CROSS"
        if direction is None:
            # Separate continuation sequence: sustained EMA trend, prior EMA9 retest,
            # and a completed resumption candle. This does not relax the cross sequence.
            n = self.pullback_trend_bars
            aligned_call = all(e9[j] > e21[j] for j in range(max(0, i - n + 1), i + 1))
            aligned_put = all(e9[j] < e21[j] for j in range(max(0, i - n + 1), i + 1))
            touch_indices = range(max(0, i - 3), i)
            # Directional retest: CALL must approach EMA9 from above and hold
            # the EMA9 zone; PUT must approach from below and hold from underneath.
            # A generic range overlap is insufficient because it accepts the same
            # candle as both bullish and bearish pullback evidence.
            call_touch = any(
                av[j] is not None
                and bars[j].o >= e9[j]
                and bars[j].l <= e9[j] + self.pullback_touch_atr * av[j]
                and bars[j].c >= e9[j] - self.pullback_touch_atr * av[j]
                for j in touch_indices
            )
            put_touch = any(
                av[j] is not None
                and bars[j].o <= e9[j]
                and bars[j].h >= e9[j] - self.pullback_touch_atr * av[j]
                and bars[j].c <= e9[j] + self.pullback_touch_atr * av[j]
                for j in touch_indices
            )
            candle_range = max(float(c.h - c.l), 1e-12)
            close_pos = (float(c.c) - float(c.l)) / candle_range
            if aligned_call and call_touch and c.c > e9[i] and c.c > c.o and close_pos >= 0.65:
                direction = "CALL"
                setup_type = "EMA9_PULLBACK_CONTINUATION"
            elif aligned_put and put_touch and c.c < e9[i] and c.c < c.o and close_pos <= 0.35:
                direction = "PUT"
                setup_type = "EMA9_PULLBACK_CONTINUATION"
            else:
                metrics["ema_cross_bars"] = None
                metrics["ema_state"] = "BULLISH" if e9[i] > e21[i] else "BEARISH" if e9[i] < e21[i] else "FLAT"
                metrics["pullback_checked"] = True
                return Decision("WAITING", None, "no fresh EMA cross or confirmed EMA9 pullback continuation", metrics)

        age = i - crossed if crossed is not None else None
        metrics["direction"] = direction
        metrics["setup_type"] = setup_type
        # The strategy adapter needs the actual origin candle, not just the
        # candle on which downstream option checks happen.
        metrics["setup_candle_open"] = bars[crossed].ts.isoformat() if crossed is not None else c.ts.isoformat()
        metrics["ema_cross_bars"] = age
        sgn = 1 if direction == "CALL" else -1
        a = float(av[i])
        if a <= 0:
            return Decision("WAITING", None, "ATR unavailable or zero", metrics)

        def reject(message):
            return Decision("REJECTED", direction, message, metrics)

        if ema_crosses > self.max_ema_crosses:
            return reject("choppy EMA regime: excessive EMA9/21 flips")
        if vwap_crosses > self.max_vwap_crosses:
            return reject("choppy VWAP regime: repeated price recrosses")
        if ema_sep_atr < self.min_ema_sep_atr:
            return reject("weak trend separation: EMA9/21 too compressed")
        if sgn * (c.c - vw[i]) <= 0:
            return reject("wrong side of session VWAP")
        if abs(c.c - vw[i]) / a > self.vwap_max_atr:
            return reject("VWAP extension too large")
        if (direction == "CALL" and rs[i] <= 50) or (direction == "PUT" and rs[i] >= 50):
            return reject("RSI direction gate failed")
        if (direction == "CALL" and rs[i] > self.rsi_call_max) or (direction == "PUT" and rs[i] < self.rsi_put_min):
            return reject("RSI overextended")
        if ad_value < self.adx_min:
            return reject(f"ADX below mandatory {self.adx_min:g}")
        if (direction == "CALL" and pdi <= ndi) or (direction == "PUT" and ndi <= pdi):
            return reject("DI direction mismatch")
        # Reject weak breakout candles: the close must hold near the directional edge,
        # and the real body must account for a meaningful share of the candle range.
        candle_range = max(float(c.h - c.l), 1e-12)
        body_ratio = abs(float(c.c - c.o)) / candle_range
        close_position = (float(c.c) - float(c.l)) / candle_range
        metrics.update({"trigger_body_ratio": round(body_ratio, 4),
                        "trigger_close_position": round(close_position, 4)})
        if body_ratio < 0.25:
            return reject("weak confirmation candle body")
        if direction == "CALL" and (c.c <= c.o or close_position < 0.62):
            return reject("bullish confirmation candle failed close-location test")
        if direction == "PUT" and (c.c >= c.o or close_position > 0.38):
            return reject("bearish confirmation candle failed close-location test")

        return Decision("UNDERLYING_SIGNAL_CONFIRMED", direction,
                        "EMA cross/pullback continuation with VWAP, RSI, ADX/DI and strong directional close confirmed", metrics)
