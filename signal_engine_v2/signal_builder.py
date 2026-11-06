"""Convert a fully validated v2 candidate into the repository's persisted Signal model."""
from __future__ import annotations

import hashlib
from datetime import datetime
from types import SimpleNamespace

from config import cfg
from engine.entry_validation import validate_entry_candidate

from engine.risk_rules import RiskRules
from models import Signal
from .contracts import ContractSelection, build_option_risk_levels
from .models import Candidate


def build_signal(candidate: Candidate, selection: ContractSelection, chain,
                 now: datetime, atr_value: float, current_underlying_price: float,
                 provider: str = "", iv_rank=None, fut_price: float = 0.0,
                 support=None, resistance=None, min_target_rr: float | None = None):
    quote, contract = selection.quote, selection.contract
    intrabar = candidate.candle_state.value == "INTRABAR"
    required_min_rr = min_target_rr if min_target_rr is not None else 1.5
    confirm_time = now.isoformat() if intrabar else candidate.candle_timestamp.isoformat()
    entry_contract = {
        "side": candidate.side.value,
        "strategy": candidate.strategy,
        "mode": "INTRABAR" if intrabar else "CLOSED_BAR",
        "setup_time": candidate.candle_timestamp.isoformat(),
        "trigger_time": candidate.candle_timestamp.isoformat(),
        "confirmation_time": confirm_time,
        "confirmation_price": float(candidate.entry),
    }
    valid, validation_reason, validation_metrics = validate_entry_candidate(
        entry_contract, SimpleNamespace(ts=candidate.candle_timestamp),
        current_underlying_price, atr_value,
        float(cfg.LIVE_MAX_ADVERSE_ATR), float(cfg.LIVE_MAX_RUNAWAY_ATR),
        structural_stop=candidate.stop, target=candidate.target1,
        min_rr=required_min_rr,
    )
    if not valid:
        return None, validation_reason
    required_rr = (min_target_rr if min_target_rr is not None
                    else validation_metrics.get("remaining_rr", candidate.risk_reward))
    levels = build_option_risk_levels(
        selection, candidate.side.value, current_underlying_price, candidate.stop,
        atr_value, min_target_rr=required_rr
    )
    if not levels:
        return None, "option_risk_levels_invalid"

    ts = (now if intrabar else candidate.candle_timestamp).isoformat(timespec="seconds")
    signal_id = hashlib.sha256(
        f"{candidate.underlying}|{candidate.side.value}|{candidate.strategy}|{candidate.setup_id}|{ts}".encode()
    ).hexdigest()[:24]
    valid_till, exit_by = RiskRules.validity(now)
    lot = max(int(contract.lot_size), 1)
    entry = float(quote.ask)
    sl, t1, t2 = float(levels["sl"]), float(levels["t1"]), float(levels["t2"])
    max_loss = max(0.0, entry - sl) * lot
    score_details = candidate.metadata.get("score_breakdown", {})
    reasons = list(candidate.reasons)
    move_potential = str(candidate.metadata.get("move_potential", "NORMAL")).upper()
    if move_potential in {"STRONG", "EXPLOSIVE"}:
        reasons.append(f"Move-potential context: {move_potential}")
    if candidate.metadata.get("impulse_direction") in (-1, 1):
        direction_label = "BULLISH" if candidate.metadata.get("impulse_direction") == 1 else "BEARISH"
        reasons.append(
            f"Impulse direction: {direction_label} "
            f"(score {candidate.metadata.get('directional_impulse_score', 0)})"
        )
    if score_details:
        reasons.append("Unified score components: " + ", ".join(
            f"{key}={value}" for key, value in score_details.items() if key != "total"
        ))

    signal = Signal(
        id=signal_id, ts=ts, underlying=candidate.underlying.upper(),
        side=candidate.side.value, contract=contract.trading_symbol,
        instrument_key=contract.instrument_key, expiry=contract.expiry.isoformat(),
        strike=float(contract.strike), opt_type=contract.opt_type,
        lot_size=lot, ltp=entry,
        entry_lo=float(levels["entry_lo"]), entry_hi=float(levels["entry_hi"]),
        sl=sl, t1=t1, t2=t2, spot=float(chain.spot),
        fut_price=float(fut_price or 0.0), und_sl=float(levels["und_sl"]),
        und_t1=float(levels["und_t1"]), und_t2=float(levels["und_t2"]),
        valid_till=valid_till, exit_by=exit_by,
        cost_lot=entry * lot, max_loss_lot=max_loss,
        rr=float(levels["rr"]), delta=abs(float(quote.delta)),
        theta=float(quote.theta or 0.0), iv=float(quote.iv or 0.0),
        iv_rank=iv_rank, spread_pct=float(quote.spread_pct),
        oi=float(quote.oi), volume=float(quote.volume),
        pcr=float(chain.pcr or 0.0),
        support=float(support if support is not None else 0.0),
        resistance=float(resistance if resistance is not None else 0.0),
        dte=max(0, (contract.expiry - now.date()).days),
        confidence=round(candidate.score / 10.0, 2),
        agree=int(candidate.metadata.get("agree", 1)),
        reasons=reasons + [f"Entry validation: {validation_metrics}"], status="ACTIVE", cur_sl=sl, prem_high=entry,
        provider=provider, strategy=candidate.strategy,
        tier="EARLY" if intrabar else "CONFIRMED",
        kind=candidate.instrument_kind.upper(), modeled_pnl=0.0,
    )
    return signal, ""
