"""Independent quality checks on an option-chain snapshot.

A recently fetched response is not proof that its quotes are fresh or usable. These checks look at the
rows the signal depends on (the ATM window) and at the chain's own spot versus the underlying's last
closed candle. They return a list of issue codes; an empty list means the chain passed.
"""
from typing import List, Optional

from config import cfg


def chain_quality_issues(chain, ref_price: Optional[float], side: Optional[str] = None) -> List[str]:
    if not chain.rows:
        return ["empty_chain"]
    issues: List[str] = []
    spot = float(chain.spot or 0.0)
    if spot <= 0:
        issues.append("chain_spot_missing")
    elif ref_price and ref_price > 0:
        dev_pct = abs(spot - ref_price) / ref_price * 100.0
        if dev_pct > cfg.CHAIN_SPOT_TOL_PCT:
            issues.append(f"spot_mismatch chain={spot:.2f} candle={ref_price:.2f} ({dev_pct:.2f}%)")

    centre = spot if spot > 0 else float(ref_price or 0.0)
    strikes = chain.strikes()
    if not strikes or centre <= 0:
        return issues + ["no_strikes_or_reference"]
    window = sorted(strikes, key=lambda s: abs(s - centre))[: 2 * cfg.CHAIN_ATM_STRIKES + 1]
    # Validate the direction actually being purchased. Missing quotes on the opposite
    # option side must not veto an otherwise executable CALL/PUT candidate.
    option_types = (("CE",) if str(side or "").upper() in ("CALL", "CE") else
                    ("PE",) if str(side or "").upper() in ("PUT", "PE") else ("CE", "PE"))
    rows = [chain.rows[(s, t)] for s in window for t in option_types if (s, t) in chain.rows]
    if not rows:
        return issues + [f"atm_rows_missing_for_{option_types[0]}"]

    crossed = sum(1 for q in rows if q.bid > 0 and q.ask > 0 and q.ask < q.bid)
    if crossed:
        issues.append(f"crossed_quotes={crossed}/{len(rows)}")
    missing = sum(1 for q in rows if q.bid <= 0 or q.ask <= 0)
    if missing / len(rows) > cfg.CHAIN_MAX_MISSING_QUOTE_FRAC:
        issues.append(f"missing_bid_ask={missing}/{len(rows)}")
    if all(q.volume <= 0 for q in rows):
        label = "zero_volume_atm" if side is None else f"zero_volume_atm_{option_types[0]}"
        issues.append(label)
    # OI is not a directional confirmation gate. Option quotes and volume remain validated for contract execution.
    return issues
