"""Strict F&O contract validation for the v2 signal engine.

The existing picker remains available to the legacy scanner. This wrapper
applies execution-quality checks before a candidate can be converted to an
option alert. It does not submit orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import cfg
from engine.contract_picker import contract_rank
from engine.risk_rules import RiskRules
from models import ChainSnapshot, Contract, OptionQuote, Underlying


@dataclass(frozen=True)
class ContractPolicy:
    max_spread_pct_index: float = 3.0
    max_spread_pct_stock: float = 4.0
    min_depth_lots: float = 1.0
    require_delta: bool = True
    require_chain_timestamp: bool = True
    max_chain_age_seconds: float = 60.0


@dataclass(frozen=True)
class ContractSelection:
    contract: Contract
    quote: OptionQuote
    reason: str = ""


def validate_option_quote(quote: OptionQuote, underlying: Underlying,
                          policy: ContractPolicy | None = None) -> tuple[bool, str]:
    policy = policy or ContractPolicy(
        max_spread_pct_index=float(cfg.MAX_SPREAD_PCT),
        max_spread_pct_stock=float(cfg.MAX_SPREAD_PCT_STOCK),
    )
    kind = str(underlying.kind).upper()
    if kind not in ("INDEX", "STOCK"):
        return False, "unsupported_underlying_kind"
    try:
        ltp, bid, ask = float(quote.ltp), float(quote.bid), float(quote.ask)
        bid_qty, ask_qty = float(quote.bid_qty), float(quote.ask_qty)
        oi, volume = float(quote.oi), float(quote.volume)
    except (TypeError, ValueError, OverflowError):
        return False, "malformed_option_quote"
    if not all(x >= 0 for x in (ltp, bid, ask, bid_qty, ask_qty, oi, volume)):
        return False, "negative_option_quote_field"
    if ltp <= 0 or bid <= 0 or ask <= 0:
        return False, "missing_executable_quote"
    if ask < float(cfg.MIN_PREMIUM):
        return False, "option_premium_below_minimum"
    if ask < bid:
        return False, "crossed_bid_ask"
    mid = (ask + bid) / 2.0
    spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else float("inf")
    spread_limit = (policy.max_spread_pct_index if kind == "INDEX"
                    else policy.max_spread_pct_stock)
    if spread_pct > spread_limit:
        return False, f"spread_too_wide:{spread_pct:.2f}%"
    lot = max(int(underlying.lot_size), 1)
    if bid_qty < lot * policy.min_depth_lots:
        return False, "insufficient_bid_depth"
    if ask_qty < lot * policy.min_depth_lots:
        return False, "insufficient_ask_depth"
    if oi / lot < cfg.MIN_OI_LOTS[kind]:
        return False, "open_interest_below_minimum"
    if volume / lot < cfg.MIN_VOLUME_LOTS[kind]:
        return False, "traded_volume_below_minimum"
    if policy.require_delta:
        try:
            delta = abs(float(quote.delta))
        except (TypeError, ValueError, OverflowError):
            return False, "greeks_delta_missing_or_invalid"
        if not 0 < delta <= 1:
            return False, "greeks_delta_missing_or_invalid"
        if not float(cfg.MIN_OPTION_DELTA) <= delta <= float(cfg.MAX_OPTION_DELTA):
            return False, "delta_outside_configured_range"
    return True, ""


def select_validated_contract(master, underlying: Underlying, side: str,
                              chain: ChainSnapshot, now: datetime,
                              iv_rank: Optional[float] = None,
                              policy: ContractPolicy | None = None
                              ) -> tuple[Optional[ContractSelection], str]:
    policy = policy or ContractPolicy(
        max_spread_pct_index=float(cfg.MAX_SPREAD_PCT),
        max_spread_pct_stock=float(cfg.MAX_SPREAD_PCT_STOCK),
    )
    side = str(side).upper()
    if side not in ("CALL", "PUT"):
        return None, "invalid_option_direction"
    if str(underlying.kind).upper() not in ("INDEX", "STOCK"):
        return None, "unsupported_underlying_kind"
    if chain.underlying.upper() != underlying.name.upper():
        return None, "chain_underlying_mismatch"
    minimum_dte = cfg.INDEX_MIN_DTE if underlying.kind.upper() == "INDEX" else cfg.STOCK_MIN_DTE
    if (chain.expiry - now.date()).days < minimum_dte:
        return None, "expiry_too_near_or_expired"
    if policy.require_chain_timestamp:
        if chain.ts is None:
            return None, "chain_timestamp_missing"
        ts = chain.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo or timezone.utc)
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        age = (current - ts).total_seconds()
        if age < -2 or age > policy.max_chain_age_seconds:
            return None, f"chain_stale:{age:.1f}s"
    if iv_rank is not None and iv_rank > cfg.MAX_IV_RANK:
        return None, "iv_rank_too_high"

    option_type = "CE" if side == "CALL" else "PE"
    strikes = sorted({strike for strike, opt_type in chain.rows if opt_type == option_type})
    if not strikes or chain.spot <= 0:
        return None, "no_option_strikes_or_spot"
    atm_index = min(range(len(strikes)), key=lambda idx: abs(strikes[idx] - chain.spot))
    itm_step = -1 if option_type == "CE" else 1
    indexes = [idx for idx in (atm_index, atm_index + itm_step, atm_index - itm_step,
                                atm_index + 2 * itm_step, atm_index - 2 * itm_step)
               if 0 <= idx < len(strikes)]
    atm_iv = None
    atm_quote = chain.rows.get((strikes[atm_index], option_type))
    if atm_quote and atm_quote.iv > 0:
        atm_iv = atm_quote.iv

    rejects = []
    for idx in sorted(indexes, key=lambda n: contract_rank(
            chain.rows[(strikes[n], option_type)], option_type, n == atm_index,
            n == atm_index + itm_step, atm_iv)):
        strike = strikes[idx]
        quote = chain.rows[(strike, option_type)]
        valid, why = validate_option_quote(quote, underlying, policy)
        if not valid:
            rejects.append(f"{strike:g}:{why}")
            continue
        contract = master.option(underlying.name, chain.expiry, strike, option_type)
        if contract is None:
            rejects.append(f"{strike:g}:not_in_instrument_master")
            continue
        if (contract.expiry != chain.expiry or contract.opt_type != option_type
                or abs(float(contract.strike) - float(strike)) > 1e-8
                or int(contract.lot_size or 0) <= 0 or float(contract.tick_size or 0) <= 0):
            rejects.append(f"{strike:g}:instrument_master_contract_mismatch")
            continue
        if not quote.key or not contract.instrument_key:
            rejects.append(f"{strike:g}:instrument_key_missing")
            continue
        if quote.key != contract.instrument_key:
            rejects.append(f"{strike:g}:instrument_key_mismatch")
            continue
        return ContractSelection(contract, quote), ""
    return None, ";".join(rejects) if rejects else "no_contract_candidate"


def build_option_risk_levels(selection: ContractSelection, side: str,
                             underlying_entry: float, underlying_stop: float,
                             atr_value: float, min_target_rr: float | None = None):
    """Create option premium SL/targets using the validated live quote delta.

    Missing delta is a hard rejection here; no assumed 0.5 delta fallback.
    """
    quote, contract = selection.quote, selection.contract
    try:
        delta = abs(float(quote.delta))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 < delta <= 1:
        return None
    stop_distance = abs(float(underlying_entry) - float(underlying_stop))
    if stop_distance <= 0:
        return None
    return RiskRules.levels(
        ltp=float(quote.ask), delta=delta, atr_val=float(atr_value),
        tick=float(contract.tick_size), side=side, spot=float(underlying_entry),
        stop_dist=stop_distance, min_target_rr=min_target_rr
    )
