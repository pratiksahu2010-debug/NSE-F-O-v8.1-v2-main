"""Pytest configuration.

The repository's older tests describe the LEGACY multi-engine behaviour (Normal Trend / MEX / ORB / live intrabar and
the original 0.40 / 0.60 ATR drift limits). The shipped default is STRATEGY_MODE=SIMPLE, so those tests pin the legacy
settings for their duration. Files named ``test_simple*.py`` run against the real defaults.
"""
import pytest

from config import cfg

_LEGACY_TEST_SETTINGS = {
    "STRATEGY_MODE": "LEGACY",
    "LIVE_INTRABAR_ENABLED": True,
    "LIVE_MAX_ADVERSE_ATR": 0.40,
    "LIVE_MAX_RUNAWAY_ATR": 0.60,
    "CHAIN_ATM_STRIKES": 3,
    "CHAIN_MAX_MISSING_QUOTE_FRAC": 0.2,
}


@pytest.fixture(autouse=True)
def _pin_legacy_settings(request):
    if request.node.fspath.basename.startswith("test_simple"):
        yield
        return
    saved = {name: getattr(cfg, name) for name in _LEGACY_TEST_SETTINGS}
    for name, value in _LEGACY_TEST_SETTINGS.items():
        setattr(cfg, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(cfg, name, value)
