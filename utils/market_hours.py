import json
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo

from config import cfg

IST = ZoneInfo("Asia/Kolkata")
OPEN = dtime(9, 15)
CLOSE = dtime(15, 30)


def now() -> datetime:
    return datetime.now(IST)


def _holidays():
    try:
        with open(cfg.HOLIDAYS_FILE) as f:
            return set(json.load(f).get("dates", []))
    except Exception:
        return set()


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _holidays()


def in_session(t: datetime) -> bool:
    return is_trading_day(t.date()) and OPEN <= t.time() <= CLOSE


def at(t: datetime, hm) -> datetime:
    return t.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def can_signal(t: datetime) -> bool:
    return is_trading_day(t.date()) and at(t, cfg.FIRST_SIGNAL_TIME) <= t <= at(t, cfg.LAST_SIGNAL_TIME)


def past_exit_time(t: datetime) -> bool:
    return t >= at(t, cfg.EXIT_TIME)


def next_scan_time(t: datetime) -> datetime:
    """Next 5-minute boundary + SCAN_DELAY_SEC."""
    step = cfg.SCAN_MINUTES
    base = t.replace(minute=0, second=0, microsecond=0)
    mins = (t.minute // step + 1) * step
    return base + timedelta(minutes=mins, seconds=cfg.SCAN_DELAY_SEC)
