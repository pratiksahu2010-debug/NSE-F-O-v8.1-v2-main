"""Balanced hybrid signal engine, introduced alongside the legacy scanner.

The package is intentionally isolated until integration and regression tests pass.
"""
from .config import EngineConfig
from .models import Candidate, Candle, MarketSnapshot, SignalDecision
from .pipeline import SignalPipeline

__all__ = ["EngineConfig", "Candidate", "Candle", "MarketSnapshot", "SignalDecision", "SignalPipeline"]

from .strategies import default_strategies
__all__.append("default_strategies")

from .runtime import create_engine, evaluate_snapshot, evaluate_legacy_candles
__all__.extend(["create_engine", "evaluate_snapshot", "evaluate_legacy_candles"])

from .runtime import build_option_signal, finalize_decision
__all__.extend(["build_option_signal", "finalize_decision"])
