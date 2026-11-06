"""Realistic outcome accounting: bid-side exit fills, round-trip costs, R-multiples and sample statistics.

The adaptive layer, the report and the calibration tool all learn from tracked outcomes, so those outcomes must not
be flattering. Entries are priced at the ask; exits are filled on the bid side (modelled half-spread slippage) and the
P&L is net of brokerage/STT/exchange/GST/stamp.
"""
from typing import Iterable, List

from config import cfg


def exit_fill_price(prem: float, spread_pct: float = 0.0) -> float:
    """Premium actually received when selling: the observed mid/LTP minus half the quoted spread (bounded)."""
    if prem <= 0:
        return prem
    sp = spread_pct if 0 < spread_pct < 900 else 0.0           # 999 = no bid/ask known
    sp = min(sp, 2.0 * cfg.EXIT_SLIP_MAX_PCT)
    slip_pct = max(cfg.EXIT_SLIP_MIN_PCT, sp / 2.0)
    return round(prem * (1.0 - slip_pct / 100.0), 2)


def round_trip_cost(entry: float, exit_px: float, lot_size: int) -> float:
    """Total rupee cost of buying then selling one lot of an option."""
    buy = max(entry, 0.0) * lot_size
    sell = max(exit_px, 0.0) * lot_size
    brokerage = 2.0 * cfg.BROKERAGE_PER_ORDER
    stt = sell * cfg.STT_SELL_PCT / 100.0
    txn = (buy + sell) * cfg.EXCH_TXN_PCT / 100.0
    sebi = (buy + sell) * cfg.SEBI_PCT / 100.0
    stamp = buy * cfg.STAMP_BUY_PCT / 100.0
    gst = (brokerage + txn + sebi) * cfg.GST_PCT / 100.0
    return brokerage + stt + txn + sebi + stamp + gst


def r_multiple(sig) -> float:
    """P&L in units of the risk taken (premium to stop). Comparable across index and stock lots, unlike rupees.
    Rows saved before max_loss_lot existed fall back to a cost-based risk estimate."""
    pnl = float(getattr(sig, "pnl_lot", 0.0) or 0.0)
    risk = float(getattr(sig, "max_loss_lot", 0.0) or 0.0)
    if risk <= 0:
        cost = float(getattr(sig, "cost_lot", 0.0) or 0.0)
        risk = cost * cfg.MAX_SL_PCT if cost > 0 else 1.0
    return pnl / risk


def profit_factor(rs: Iterable[float]) -> float:
    rs = list(rs)
    gain = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r < 0)
    if loss <= 0:
        return float("inf") if gain > 0 else 1.0
    return gain / loss


def summarize(rs: List[float]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0, "win_pct": 0.0, "avg_r": 0.0, "pf": 0.0, "total_r": 0.0, "max_lose_streak": 0}
    streak = best = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        best = max(best, streak)
    pf = profit_factor(rs)
    return {"n": n, "win_pct": round(100.0 * sum(1 for r in rs if r > 0) / n, 1),
            "avg_r": round(sum(rs) / n, 3), "pf": 99.0 if pf == float("inf") else round(pf, 2),
            "total_r": round(sum(rs), 2), "max_lose_streak": best}
