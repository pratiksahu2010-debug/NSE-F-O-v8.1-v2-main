"""Summarise paper results from the signal DB: python tools/paper_report.py [days]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import DB  # noqa: E402

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    rows = [(r["day"], r["und"], r["side"], r["data"]) for r in DB()._q("SELECT day, und, side, data FROM signals ORDER BY day DESC LIMIT 2000")]
    tot = wins = losses = 0
    pnl = 0.0
    by_und = {}
    for day, und, side, data in rows[: days * 20]:
        d = json.loads(data)
        if d.get("status") != "CLOSED":
            continue
        tot += 1
        p = d.get("pnl_lot", 0)
        pnl += p
        wins += p > 0
        losses += p < 0
        b = by_und.setdefault(und, [0, 0.0])
        b[0] += 1
        b[1] += p
    print(f"Closed signals: {tot} | wins {wins} | losses {losses} | win rate {100 * wins / tot:.0f}%" if tot else "No closed signals yet")
    print(f"Total P&L (1 lot each, before brokerage/taxes): Rs {pnl:,.0f}")
    for und, (n, p) in sorted(by_und.items(), key=lambda x: x[1][1]):
        print(f"  {und:12s} n={n:3d}  pnl={p:,.0f}")
