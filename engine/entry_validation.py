from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math


def _dt(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _cmp(value):
    # Compare absolute instants; do not discard offsets (e.g. IST vs UTC).
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    strategy: str
    side: str
    setup_time: str
    trigger_time: str
    confirmation_time: str
    confirmation_price: float
    timeframe: str = "5m"
    setup_id: str = ""

    def to_dict(self):
        return asdict(self)


def validate_entry_candidate(candidate, latest_candle, current_price, atr_value,
                             max_adverse_atr, max_runaway_atr,
                             structural_stop=None, target=None, min_rr=None):
    """Validate chronology, latest-bar confirmation, price displacement and structural RR."""
    if not isinstance(candidate, dict):
        return False, "MISSING_ENTRY_CONTRACT", {}
    side = str(candidate.get("side", "")).upper()
    if side not in ("CALL", "PUT"):
        return False, "INVALID_DIRECTION", {}
    sign = 1 if side == "CALL" else -1
    setup, trigger, confirm = (_dt(candidate.get(k)) for k in ("setup_time", "trigger_time", "confirmation_time"))
    try:
        ref, spot, atrv = (float(x) for x in (candidate.get("confirmation_price"), current_price, atr_value))
    except (TypeError, ValueError, OverflowError):
        return False, "INVALID_ENTRY_REFERENCE", {}
    if not all(math.isfinite(x) and x > 0 for x in (ref, spot, atrv)):
        return False, "INVALID_ENTRY_REFERENCE", {}
    latest = getattr(latest_candle, "ts", None)
    if any(x is None for x in (setup, trigger, confirm)) or not isinstance(latest, datetime):
        return False, "INVALID_ENTRY_TIMESTAMPS", {}
    intrabar = str(candidate.get("mode", "")).upper() == "INTRABAR"
    if not intrabar and _cmp(confirm) != _cmp(latest):
        return False, "STALE_CONFIRMATION_CANDLE", {"confirmation_time": confirm.isoformat(), "latest_candle_time": latest.isoformat()}
    if intrabar:
        # Intrabar candidates are timestamped against a distinct live partial bar.
        # Their freshness is measured from the live confirmation time, not by
        # pretending that the partial candle is a completed candle.
        age = (datetime.now(timezone.utc) - _cmp(confirm)).total_seconds()
        if age < -2 or age > 8:
            return False, "STALE_LIVE_TRIGGER", {"confirmation_age_sec": round(age, 3)}
    if _cmp(setup) > _cmp(trigger) or _cmp(trigger) > _cmp(confirm):
        return False, "INVALID_ENTRY_TIMESTAMP_ORDER", {}
    displacement = sign * (spot - ref) / atrv
    metrics = {"strategy": candidate.get("strategy"), "mode": candidate.get("mode", "CLOSED_BAR"), "setup_time": setup.isoformat(),
               "trigger_time": trigger.isoformat(), "confirmation_time": confirm.isoformat(),
               "confirmation_price": ref, "current_price": spot, "displacement_atr": round(displacement, 4)}
    if displacement < -abs(float(max_adverse_atr)):
        return False, "ADVERSE_MOVE_AFTER_CONFIRMATION", metrics
    if displacement > abs(float(max_runaway_atr)):
        return False, "PRICE_EXTENDED_AFTER_CONFIRMATION", metrics
    if structural_stop is not None or target is not None:
        try:
            stop, objective = float(structural_stop), float(target)
        except (TypeError, ValueError, OverflowError):
            return False, "INVALID_STRUCTURAL_LEVELS", metrics
        if not all(math.isfinite(x) and x > 0 for x in (stop, objective)):
            return False, "INVALID_STRUCTURAL_LEVELS", metrics
        risk = spot - stop if sign > 0 else stop - spot
        reward = objective - spot if sign > 0 else spot - objective
        if risk <= 0 or reward <= 0:
            return False, "INVALID_REMAINING_TRADE_STRUCTURE", metrics
        rr = reward / risk
        metrics.update({"structural_stop": stop, "target": objective, "remaining_risk": risk,
                        "remaining_reward": reward, "remaining_rr": round(rr, 4)})
        if min_rr is not None and rr + 1e-9 < float(min_rr):
            return False, "INSUFFICIENT_REMAINING_REWARD_RISK", metrics
    return True, "ENTRY_VALID", metrics
