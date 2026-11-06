from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


@dataclass
class Candle:
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0
    oi: float = 0.0


@dataclass
class Underlying:
    name: str                 # NIFTY, BANKNIFTY, RELIANCE ...
    kind: str                 # INDEX | STOCK
    spot_key: str             # Upstox instrument key of the spot/index
    fut_key: str = ""         # Upstox key of the current-month future
    lot_size: int = 0
    strike_step: float = 0.0
    dhan_id: int = 0          # Dhan security id of the spot/index
    dhan_seg: str = ""        # IDX_I | NSE_EQ
    group: str = ""           # correlation group (BANK, IT, ...)


@dataclass
class Contract:
    underlying: str
    expiry: date
    strike: float
    opt_type: str             # CE | PE
    instrument_key: str
    trading_symbol: str
    lot_size: int
    tick_size: float = 0.05


@dataclass
class OptionQuote:
    strike: float
    opt_type: str
    key: str = ""
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    oi: float = 0.0
    prev_oi: float = 0.0
    volume: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    theta: float = 0.0

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 999.0
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100


@dataclass
class ChainSnapshot:
    underlying: str
    expiry: date
    spot: float
    pcr: float
    rows: dict = field(default_factory=dict)     # (strike, 'CE'|'PE') -> OptionQuote
    ts: Optional[datetime] = None

    def strikes(self):
        return sorted({k[0] for k in self.rows})

    def total_oi(self, opt_type):
        return sum(q.oi for (s, t), q in self.rows.items() if t == opt_type)

    def calc_pcr(self):
        c = self.total_oi("CE")
        return self.total_oi("PE") / c if c else 0.0

    def wall(self, opt_type, spot=None):
        """Strike with max OI: calls above spot (resistance), puts below spot (support)."""
        spot = spot if spot is not None else self.spot
        best = None
        for (s, t), q in self.rows.items():
            if t != opt_type:
                continue
            if opt_type == "CE" and s < spot:
                continue
            if opt_type == "PE" and s > spot:
                continue
            if best is None or q.oi > best[1]:
                best = (s, q.oi)
        return best  # (strike, oi) or None

    def atm_iv(self):
        if not self.rows:
            return 0.0
        strikes = self.strikes()
        atm = min(strikes, key=lambda s: abs(s - self.spot))
        ivs = [self.rows[(atm, t)].iv for t in ("CE", "PE") if (atm, t) in self.rows and self.rows[(atm, t)].iv > 0]
        return sum(ivs) / len(ivs) if ivs else 0.0


@dataclass
class Vote:
    name: str
    score: float              # -1 (PUT) .. +1 (CALL)
    reason: str


@dataclass
class Signal:
    id: str = ""
    ts: str = ""
    underlying: str = ""
    side: str = ""            # CALL | PUT
    contract: str = ""        # trading symbol
    instrument_key: str = ""
    expiry: str = ""
    strike: float = 0.0
    opt_type: str = ""
    lot_size: int = 0
    ltp: float = 0.0
    entry_lo: float = 0.0
    entry_hi: float = 0.0
    sl: float = 0.0
    t1: float = 0.0
    t2: float = 0.0
    t3: float = 0.0
    spot: float = 0.0
    fut_price: float = 0.0
    und_sl: float = 0.0
    und_t1: float = 0.0
    und_t2: float = 0.0
    und_t3: float = 0.0
    valid_till: str = ""
    exit_by: str = ""
    cost_lot: float = 0.0
    max_loss_lot: float = 0.0
    rr: float = 0.0
    delta: float = 0.0
    theta: float = 0.0
    iv: float = 0.0
    iv_rank: Optional[float] = None
    spread_pct: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    pcr: float = 0.0
    support: float = 0.0
    resistance: float = 0.0
    dte: int = 0
    confidence: float = 0.0
    agree: int = 0
    reasons: list = field(default_factory=list)
    # tracking
    status: str = "ACTIVE"    # ACTIVE | T1_HIT | T2_HIT | CLOSED
    cur_sl: float = 0.0
    prem_high: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    result: str = ""          # WIN | LOSS | BREAKEVEN | TIME_EXIT
    pnl_lot: float = 0.0
    provider: str = ""
    strategy: str = "TREND"       # TREND | REVERSAL
    tier: str = "CONFIRMED"       # CONFIRMED | EARLY (preview-only; not persisted/tracked)
    kind: str = ""                # INDEX | STOCK (so outcomes can be learned per kind)
    modeled_pnl: float = 0.0       # kept for DB compatibility; equals pnl_lot (one lot)

    def to_dict(self):
        return asdict(self)
