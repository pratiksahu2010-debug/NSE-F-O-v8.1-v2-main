import math
from datetime import datetime, timedelta
from typing import Optional

from config import cfg
from utils.market_hours import at


def _round_tick(x: float, tick: float, up: bool = False) -> float:
    tick = tick or 0.05
    n = x / tick
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * tick, 2)


class RiskRules:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def levels(ltp: float, delta: float, atr_val: float, tick: float, side: str, spot: float,
               stop_dist: Optional[float] = None, min_target_rr: Optional[float] = None) -> Optional[dict]:
        """Build option-premium levels from one consistent underlying risk distance.

        A structural stop is never silently widened or narrowed to fit an ATR band.
        Invalid/too-wide structure is rejected. The premium stop is delta-estimated
        from that same distance, and the underlying SL is kept mathematically aligned.
        """
        try:
            ltp, atr_val, spot = float(ltp), float(atr_val), float(spot)
            delta = abs(float(delta or 0.5))
        except (TypeError, ValueError, OverflowError):
            return None
        if not all(math.isfinite(x) and x > 0 for x in (ltp, atr_val, spot)):
            return None
        if not math.isfinite(delta) or delta <= 0:
            return None
        d = min(0.95, max(delta, 0.35))
        sgn = 1 if str(side).upper() == "CALL" else -1
        if str(side).upper() not in ("CALL", "PUT"):
            return None
        min_dist = float(cfg.SL_MIN_ATR) * atr_val
        max_dist = float(cfg.SL_MAX_ATR) * atr_val
        if stop_dist is None or not math.isfinite(float(stop_dist)) or float(stop_dist) <= 0:
            sl_d = float(cfg.SL_ATR) * atr_val
        else:
            raw_dist = float(stop_dist)
            # A tiny structural gap gets a volatility buffer; a genuinely wide
            # structural stop is rejected rather than moved artificially closer.
            if raw_dist > max_dist:
                return None
            sl_d = max(raw_dist, min_dist)
        if sl_d <= 0 or sl_d > max_dist:
            return None
        premium_risk = d * sl_d
        # Do not cap the option stop independently: doing so made the option SL
        # disagree with the underlying invalidation level. Reject if the true
        # delta-estimated loss is beyond the configured premium-risk ceiling.
        if premium_risk >= ltp * float(cfg.MAX_SL_PCT):
            return None
        t1_d = max(float(cfg.T1_ATR) * atr_val, float(cfg.T1_R) * sl_d,
                   max(0.0, float(min_target_rr or 0.0)) * sl_d)
        t2_d = max(float(cfg.T2_ATR) * atr_val, float(cfg.T2_R) * sl_d)
        t3_d = max(float(cfg.T3_R) * sl_d, float(cfg.T2_R) * sl_d)
        sl = _round_tick(ltp - premium_risk, tick)
        t1 = _round_tick(ltp + d * t1_d, tick, up=True)
        t2 = _round_tick(ltp + d * t2_d, tick, up=True)
        t3 = _round_tick(ltp + d * t3_d, tick, up=True)
        risk = ltp - sl
        if risk <= 0:
            return None
        rr1, rr2 = (t1 - ltp) / risk, (t2 - ltp) / risk
        if rr1 + 1e-9 < float(cfg.MIN_RR_T1):
            return None
        return {
            "sl": sl, "t1": t1, "t2": t2, "t3": t3, "rr": rr2,
            "entry_lo": _round_tick(ltp * (1 - cfg.ENTRY_BAND_DOWN), tick),
            "entry_hi": _round_tick(ltp * (1 + cfg.ENTRY_BAND_UP), tick, up=True),
            "und_sl": spot - sgn * sl_d, "und_t1": spot + sgn * t1_d, "und_t2": spot + sgn * t2_d,
            "und_t3": spot + sgn * t3_d,
            "underlying_stop_distance": sl_d, "premium_risk_estimate": premium_risk,
        }
    @staticmethod
    def validity(now: datetime):
        valid = min(now + timedelta(minutes=cfg.VALID_MINUTES), at(now, cfg.LAST_SIGNAL_TIME))
        return valid.strftime("%H:%M"), at(now, cfg.EXIT_TIME).strftime("%H:%M")
