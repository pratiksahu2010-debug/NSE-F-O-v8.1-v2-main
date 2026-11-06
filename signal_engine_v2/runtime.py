"""Side-effect-free orchestration for the new signal engine.

The caller owns feed polling, database persistence and Telegram dispatch.
There is deliberately no broker/order-routing integration in this package.
"""
from __future__ import annotations

from typing import Optional, Sequence

from .adapter import build_snapshot
from .config import EngineConfig
from .models import DecisionState, SignalDecision
from .pipeline import SignalPipeline
from .strategies import default_strategies
from .contracts import ContractPolicy, select_validated_contract
from .signal_builder import build_signal
from engine.chain_quality import chain_quality_issues


def create_engine(strategies: Optional[Sequence] = None,
                  config: Optional[EngineConfig] = None) -> SignalPipeline:
    return SignalPipeline(strategies or default_strategies(), config or EngineConfig())


def evaluate_snapshot(engine: SignalPipeline, snapshot) -> list[SignalDecision]:
    """Evaluate strategy candidates and resolve conflicts without portfolio caps."""
    return engine.evaluate(snapshot)


def evaluate_legacy_candles(engine: SignalPipeline, underlying: str,
                            instrument_kind: str, timeframe: str,
                            candles, live_candle=None,
                            futures_volume_by_timestamp=None,
                            session_candles=None, metadata=None) -> list[SignalDecision]:
    snapshot = build_snapshot(
        underlying, instrument_kind, timeframe, candles,
        live_candle=live_candle,
        futures_volume_by_timestamp=futures_volume_by_timestamp,
        session_candles=session_candles,
        metadata=metadata,
    )
    return evaluate_snapshot(engine, snapshot)


def build_option_signal(candidate, master, underlying, chain, now,
                        atr_value: float, current_underlying_price: float,
                        provider: str = "", iv_rank=None, fut_price: float = 0.0,
                        policy: Optional[ContractPolicy] = None):
    """Validate chain, select an executable contract and build a Signal record.

    The returned Signal is ready for the existing DB/tracker/outbox layer. The
    caller remains responsible for persistence and Telegram dispatch.
    """
    issues = chain_quality_issues(chain, current_underlying_price, candidate.side.value)
    if issues:
        return None, "chain_quality:" + ",".join(issues)
    selection, reason = select_validated_contract(
        master, underlying, candidate.side.value, chain, now,
        iv_rank=iv_rank, policy=policy,
    )
    if selection is None:
        return None, "contract_selection:" + str(reason)
    return build_signal(
        candidate, selection, chain, now, atr_value,
        current_underlying_price, provider=provider, iv_rank=iv_rank,
        fut_price=fut_price, min_target_rr=1.5,
    )


def finalize_decision(engine: SignalPipeline, decision: SignalDecision,
                      master, underlying, chain, now,
                      atr_value: float, current_underlying_price: float,
                      provider: str = "", iv_rank=None, fut_price: float = 0.0,
                      policy: Optional[ContractPolicy] = None):
    """Run final chain/contract/risk checks; release dedup reservation on failure."""
    if decision.state is DecisionState.REJECTED:
        return None, "candidate_already_rejected"
    signal, reason = build_option_signal(
        decision.candidate, master, underlying, chain, now, atr_value,
        current_underlying_price, provider, iv_rank, fut_price, policy,
    )
    if signal is None:
        engine.release(decision)
    return signal, reason

