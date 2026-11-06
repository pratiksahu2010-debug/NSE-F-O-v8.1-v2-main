from typing import Protocol, Sequence
from .models import Candidate, MarketSnapshot


class Strategy(Protocol):
    """A strategy detects setups; it must never send Telegram alerts itself."""
    name: str
    priority: int

    def evaluate(self, snapshot: MarketSnapshot) -> Sequence[Candidate]:
        ...
