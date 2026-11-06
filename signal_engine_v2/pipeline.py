from __future__ import annotations

from collections import defaultdict
from threading import RLock
from typing import Iterable, Sequence

from .config import EngineConfig
from .models import (Candidate, CandleState, DecisionState, MarketSnapshot,
                     SignalDecision, directional_levels_valid)
from .strategy import Strategy
from .scorer import score_candidate


class SignalPipeline:
    """Collect, validate and select candidates from independent strategies.

    This layer is deliberately side-effect free: it does not place orders,
    fetch data, select option contracts or send alerts.
    """
    def __init__(self, strategies: Sequence[Strategy], config: EngineConfig | None = None):
        self.strategies = tuple(strategies)
        self.config = config or EngineConfig()
        self._emitted: set[str] = set()
        self._emitted_session_date = None
        self._lock = RLock()

    def collect(self, snapshot: MarketSnapshot) -> list[Candidate]:
        candidates: list[Candidate] = []
        for strategy in self.strategies:
            try:
                found = strategy.evaluate(snapshot) or ()
                for item in found:
                    if not isinstance(item, Candidate):
                        continue
                    if item.underlying.upper() != snapshot.underlying.upper():
                        continue
                    candidates.append(item)
            except Exception:
                # A broken strategy must not stop other independent strategies.
                continue
        return candidates

    def _reject_reasons(self, c: Candidate, snapshot: MarketSnapshot) -> tuple[str, ...]:
        reasons: list[str] = []
        if len([bar for bar in snapshot.candles if bar.is_closed]) < self.config.min_history_bars:
            reasons.append("insufficient_220_bar_history")
        if not 0 <= c.score <= 100 or c.score < self.config.min_score:
            reasons.append("score_below_minimum")
        adx_value = snapshot.indicators.get("adx_14")
        if not isinstance(adx_value, (int, float)) or adx_value < self.config.min_adx:
            reasons.append(f"adx_below_minimum_{self.config.min_adx:g}")
        # Avoid late entries after price has travelled too far from the
        # short-term trend mean. This is an entry-quality gate, not a signal cap.
        atr_value = snapshot.indicators.get("atr_14")
        ema21 = snapshot.indicators.get("ema_21")
        if isinstance(atr_value, (int, float)) and atr_value > 0 and isinstance(ema21, (int, float)):
            extension = ((c.entry - ema21) if c.side.value == "CALL" else (ema21 - c.entry)) / atr_value
            if extension > self.config.max_entry_extension_atr:
                reasons.append("entry_overextended_from_ema21")
        if c.candle_timestamp != snapshot.candle.timestamp:
            reasons.append("candidate_confirmation_timestamp_mismatch")
        if c.age_bars > self.config.max_candidate_age_bars:
            reasons.append("setup_too_old")
        if not directional_levels_valid(c):
            reasons.append("invalid_entry_stop_target_structure")
        if self.config.require_valid_stop and c.risk_reward < self.config.min_risk_reward:
            reasons.append("risk_reward_below_minimum")
        if c.candle_state is CandleState.CLOSED:
            if not snapshot.candle.is_closed:
                reasons.append("closed_candidate_without_closed_snapshot")
            if not self.config.allow_closed_bar_candidates:
                reasons.append("closed_bar_candidates_disabled")
        else:
            if not self.config.allow_intrabar_candidates:
                reasons.append("intrabar_candidates_disabled")
        if self.config.require_closed_htf and c.htf_confirmed and any(not x.is_closed for x in snapshot.htf_candles):
            reasons.append("higher_timeframe_contains_unclosed_candle")
        return tuple(reasons)

    def _rank(self, c: Candidate) -> tuple[int, float, int, str]:
        # A closed-bar confirmation always outranks an early intrabar preview
        # for the same underlying/candle. Strategy priority breaks score ties.
        closed_first = 1 if c.candle_state is CandleState.CLOSED else 0
        return (closed_first, c.score, -c.strategy_priority, c.strategy.upper())

    def resolve(self, candidates: Iterable[Candidate], snapshot: MarketSnapshot) -> list[SignalDecision]:
        valid: list[Candidate] = []
        rejected: list[SignalDecision] = []
        for original in candidates:
            c = score_candidate(original, snapshot)[0] if self.config.unified_scoring_enabled else original
            reasons = self._reject_reasons(c, snapshot)
            if reasons:
                rejected.append(SignalDecision(c, DecisionState.REJECTED, c.identity, reasons))
            else:
                valid.append(c)

        # One alert per underlying + candle by default. Opposite-side setups
        # compete in the same group and cannot both be published.
        groups: dict[str, list[Candidate]] = defaultdict(list)
        for c in valid:
            key = f"{c.underlying.upper()}|{c.candle_timestamp.isoformat()}"
            groups[key].append(c)

        selected: list[SignalDecision] = []
        for group_key, group in groups.items():
            group.sort(key=self._rank, reverse=True)
            winner = group[0]
            if len(group) > 1:
                # Compare opposing candidates of the same confirmation class.
                # An early intrabar preview must never veto a closed-bar signal.
                opposing = next((x for x in group[1:]
                                 if x.candle_state is winner.candle_state and x.side != winner.side), None)
                if opposing is not None and winner.score - opposing.score < self.config.conflict_score_margin:
                    rejected.extend(SignalDecision(x, DecisionState.REJECTED, x.identity,
                                      ("directional_conflict_no_clear_winner",)) for x in group)
                    continue
            state = (DecisionState.CONFIRMED if winner.candle_state is CandleState.CLOSED
                     else DecisionState.EARLY)
            selected.append(SignalDecision(winner, state, group_key))
            rejected.extend(SignalDecision(x, DecisionState.REJECTED, x.identity,
                              ("superseded_by_unified_selector",)) for x in group[1:])

        # Idempotency protects repeated scans of the same candle/setup. Clear
        # yesterday's keys when the trading date advances to keep a long-lived
        # Render worker's memory bounded.
        fresh: list[SignalDecision] = []
        session_date = snapshot.candle.timestamp.date()
        with self._lock:
            if self._emitted_session_date != session_date:
                self._emitted.clear()
                self._emitted_session_date = session_date
            for decision in selected:
                dedup_key = decision.decision_key + "|" + decision.state.value
                if dedup_key in self._emitted:
                    continue
                self._emitted.add(dedup_key)
                fresh.append(decision)
        return rejected + fresh

    def release(self, decision: SignalDecision) -> None:
        """Allow a same-candle retry when downstream contract validation fails."""
        dedup_key = decision.decision_key + "|" + decision.state.value
        with self._lock:
            self._emitted.discard(dedup_key)

    def evaluate(self, snapshot: MarketSnapshot) -> list[SignalDecision]:
        return self.resolve(self.collect(snapshot), snapshot)
