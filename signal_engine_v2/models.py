from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Optional


class Side(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class CandleState(str, Enum):
    CLOSED = "CLOSED"
    INTRABAR = "INTRABAR"


class DecisionState(str, Enum):
    CONFIRMED = "CLOSED_BAR_CONFIRMED"
    EARLY = "INTRABAR_EARLY"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    is_closed: bool = True

    def valid(self) -> bool:
        values = (self.open, self.high, self.low, self.close, self.volume)
        return (all(isinstance(x, (int, float)) for x in values)
                and all(x > 0 for x in values[:4]) and self.volume >= 0
                and self.high >= max(self.open, self.close, self.low)
                and self.low <= min(self.open, self.close, self.high))


@dataclass(frozen=True)
class MarketSnapshot:
    underlying: str
    instrument_kind: str  # INDEX | STOCK
    timeframe: str
    candle: Candle
    candles: tuple[Candle, ...] = ()
    htf_candles: tuple[Candle, ...] = ()  # completed higher-timeframe candles only
    indicators: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    underlying: str
    instrument_kind: str
    side: Side
    strategy: str
    setup_id: str
    candle_timestamp: datetime
    candle_state: CandleState
    score: float
    entry: float
    stop: float
    target1: float
    target2: Optional[float] = None
    reasons: tuple[str, ...] = ()
    strategy_priority: int = 100  # lower number wins ties
    age_bars: int = 0
    htf_confirmed: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def risk_reward(self) -> float:
        risk = abs(self.entry - self.stop)
        reward = abs(self.target1 - self.entry)
        return reward / risk if risk > 0 else 0.0

    @property
    def identity(self) -> str:
        return "|".join((self.underlying.upper(), self.strategy.upper(), self.setup_id,
                         self.side.value, self.candle_timestamp.isoformat()))


@dataclass(frozen=True)
class SignalDecision:
    candidate: Candidate
    state: DecisionState
    decision_key: str
    rejected_reasons: tuple[str, ...] = ()


def directional_levels_valid(candidate: Candidate) -> bool:
    if candidate.side is Side.CALL:
        return candidate.stop < candidate.entry < candidate.target1
    return candidate.target1 < candidate.entry < candidate.stop
