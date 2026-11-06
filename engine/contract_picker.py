import logging
from datetime import date, datetime
from typing import List, Optional, Tuple

from config import cfg
from data.instruments import Master
from models import ChainSnapshot, Contract, OptionQuote, Underlying
from utils.market_hours import at

log = logging.getLogger("picker")


def pick_expiry(master: Master, und: Underlying, today: date) -> Optional[date]:
    exps = [e for e in master.expiries.get(und.name, []) if e >= today]
    if not exps:
        return None
    if und.kind == "INDEX":
        for e in exps:                       # nearest, else next, with enough days left
            if (e - today).days >= cfg.INDEX_MIN_DTE:
                return e
        return None
    monthly = {}                              # last expiry in each month = monthly expiry
    for e in exps:
        monthly[(e.year, e.month)] = e
    for e in sorted(monthly.values()):
        if (e - today).days >= cfg.STOCK_MIN_DTE:
            return e
    return None


def liquidity_ok(q: OptionQuote, und: Underlying, lot: int) -> Tuple[bool, str]:
    lot = max(lot, 1)
    if q.ltp < cfg.MIN_PREMIUM:
        return False, f"premium {q.ltp:.2f} < {cfg.MIN_PREMIUM}"
    if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
        return False, "missing or crossed executable bid/ask"
    if q.oi / lot < cfg.MIN_OI_LOTS[und.kind]:
        return False, f"OI {q.oi / lot:.0f} lots"
    if q.volume / lot < cfg.MIN_VOLUME_LOTS[und.kind]:
        return False, f"volume {q.volume / lot:.0f} lots"
    return True, ""


def _atm_iv(chain: ChainSnapshot, ot: str) -> Optional[float]:
    """IV of the strike nearest spot on this option side (0/None when the chain carries no IV)."""
    strikes = sorted({s for (s, t) in chain.rows if t == ot})
    if not strikes:
        return None
    s = min(strikes, key=lambda x: abs(x - chain.spot))
    iv = float(chain.rows[(s, ot)].iv or 0.0)
    return iv if iv > 0 else None


def contract_rank(q: OptionQuote, ot: str, is_atm: bool, is_itm: bool, atm_iv: Optional[float]) -> float:
    """Lower is better. Delta fit is primary; spread and IV dislocation are tie-breakers. Spread is also gated
    in liquidity_ok; here it only breaks ties. Strikes without delta fall back to moneyness order."""
    if q.delta:
        base = abs(abs(q.delta) - cfg.TARGET_DELTA)
    else:
        base = 0.0 if is_atm else (0.1 if is_itm else 0.3)
    spread_pct = (q.ask - q.bid) / q.ltp * 100.0 if (q.bid > 0 and q.ask > q.bid and q.ltp > 0) else 5.0
    penalty = 0.01 * min(spread_pct, 20.0)
    if atm_iv and q.iv:
        penalty += 0.005 * min(abs(q.iv - atm_iv) / atm_iv * 100.0, 30.0)
    return base + penalty


def pick_contract(master: Master, und: Underlying, side: str, chain: ChainSnapshot, now: datetime,
                  iv_rank: Optional[float] = None) -> Tuple[Optional[Contract], Optional[OptionQuote], str]:
    ot = "CE" if side == "CALL" else "PE"
    strikes = sorted({s for (s, t) in chain.rows if t == ot})
    if not strikes:
        return None, None, "no strikes"
    ai = min(range(len(strikes)), key=lambda i: abs(strikes[i] - chain.spot))
    itm = -1 if ot == "CE" else 1
    cand_idx = [i for i in (ai, ai + itm, ai - itm, ai + 2 * itm, ai - 2 * itm)
                if 0 <= i < len(strikes)]   # ATM, up to 2 ITM and 2 OTM; quality-ranked, not blindly nearest

    atm_iv = _atm_iv(chain, ot)

    def rank(i):
        return contract_rank(chain.rows[(strikes[i], ot)], ot, i == ai, i == ai + itm, atm_iv)

    if iv_rank is not None and iv_rank > cfg.MAX_IV_RANK:
        return None, None, f"IV rank {iv_rank:.0f} too high to buy options"
    reject = []
    for i in sorted(cand_idx, key=rank):
        q = chain.rows[(strikes[i], ot)]
        if q.delta is not None and abs(float(q.delta)) > 0:
            delta_abs = abs(float(q.delta))
            if delta_abs < float(cfg.MIN_OPTION_DELTA) or delta_abs > float(cfg.MAX_OPTION_DELTA):
                reject.append(f"{strikes[i]:g}: delta {delta_abs:.2f} outside {cfg.MIN_OPTION_DELTA:.2f}-{cfg.MAX_OPTION_DELTA:.2f}")
                continue
        ok, why = liquidity_ok(q, und, und.lot_size)
        if not ok:
            reject.append(f"{strikes[i]:g}: {why}")
            continue
        c = master.option(und.name, chain.expiry, strikes[i], ot)
        if not c:
            reject.append(f"{strikes[i]:g}: not in master")
            continue
        if chain.expiry == now.date() and now >= at(now, (14, 30)):
            return None, None, "expiry day after 14:30 (theta)"
        return c, q, ""
    return None, None, "; ".join(reject) or "no candidate"
