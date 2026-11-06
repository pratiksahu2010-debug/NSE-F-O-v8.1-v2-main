"""Calendar event mute list (results, RBI, budget...)."""
import json
from datetime import date

from config import cfg


def load_events() -> dict:
    try:
        with open(cfg.EVENTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"global_mute_dates": {}, "symbol_events": {}}


def global_mute(d: date) -> str:
    return load_events().get("global_mute_dates", {}).get(d.isoformat(), "")


def symbol_event(name: str, d: date) -> bool:
    return d.isoformat() in load_events().get("symbol_events", {}).get(name, [])
