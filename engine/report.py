"""Performance report over closed signals: which signal types / tiers / score bands / hours actually work."""
import html
from datetime import timedelta
from typing import Callable, Dict, List

from engine.adaptive import _pf

INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def _stats(sigs) -> dict:
    pnls = [s.pnl_lot for s in sigs]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    pf = _pf(pnls) if n else 0.0
    return {"n": n, "wins": wins, "win_pct": round(100.0 * wins / n, 1) if n else 0.0,
            "pf": round(pf, 2) if pf != float("inf") else 99.0, "avg_pnl": round(sum(pnls) / n, 0) if n else 0.0,
            "total_pnl": round(sum(pnls), 0)}


def _band(c: float) -> str:
    return "90+" if c >= 90 else "80-89" if c >= 80 else "72-79" if c >= 72 else "<72"


def _group(sigs, key: Callable) -> Dict[str, dict]:
    g: Dict[str, list] = {}
    for s in sigs:
        g.setdefault(key(s), []).append(s)
    return {k: _stats(v) for k, v in sorted(g.items())}


def build(db, days: int, today) -> dict:
    start = (today - timedelta(days=days)).isoformat()
    sigs = db.closed_since(start)
    return {
        "days": days, "overall": _stats(sigs),
        "by_strategy": _group(sigs, lambda s: s.strategy or "TREND"),
        "by_tier": _group(sigs, lambda s: s.tier or "FULL"),
        "by_score": _group(sigs, lambda s: _band(s.confidence)),
        "by_hour": _group(sigs, lambda s: (s.ts[11:13] + ":00") if len(s.ts) >= 13 else "?"),
        "by_side": _group(sigs, lambda s: s.side),
        "by_kind": _group(sigs, lambda s: "Index" if s.underlying in INDEX_NAMES else "Stock"),
    }


def _line(name: str, st: dict) -> str:
    pf = "∞" if st["pf"] >= 99 else f"{st['pf']:.2f}"
    return f"• {html.escape(name)}: {st['n']} sig | win {st['win_pct']:.0f}% | PF {pf} | avg ₹{st['avg_pnl']:,.0f}/lot"


def format_report(rep: dict, adaptive: Dict[str, str] = None, compact: bool = False) -> str:
    o = rep["overall"]
    if not o["n"]:
        return f"📈 <b>Performance, last {rep['days']} days</b>\nNo closed signals yet."
    lines = [f"📈 <b>Performance, last {rep['days']} days</b> (paper, per 1 lot)", _line("All", o)]
    lines.append("<b>By signal type</b>")
    lines += [_line(k, v) for k, v in rep["by_strategy"].items()]
    if compact:
        return "\n".join(lines)
    for title, key in (("By tier", "by_tier"), ("By score", "by_score"), ("By hour", "by_hour"),
                       ("By side", "by_side"), ("Index vs stock", "by_kind")):
        lines.append(f"<b>{title}</b>")
        lines += [_line(k, v) for k, v in rep[key].items()]
    if adaptive:
        lines.append("<b>Adaptive bar (auto-adjusts per type)</b>")
        lines += [f"• {html.escape(k)}: {v}" for k, v in sorted(adaptive.items())]
    lines.append("<i>PF = profit factor (gross profit / gross loss). Judge types only after ~30 closed signals each.</i>")
    return "\n".join(lines)
