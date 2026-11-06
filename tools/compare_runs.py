"""Compare CLOSED signal outcomes from two bot databases (not a candle-level backtest).

Usage: python tools/compare_runs.py baseline.db optimized.db [--days 30]
Both databases must contain closed signals with pnl_lot recorded. P&L is per lot and excludes costs unless the
signal tracker has already incorporated them. Use separate baseline/optimized DB copies for an apples-to-apples run.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import DB  # noqa: E402


def stats(path, days):
    db = DB(path=path, url="")
    try:
        rows = db._q("SELECT ts, status, data FROM signals ORDER BY ts")
        values = []
        for row in rows:
            d = json.loads(row["data"])
            if d.get("status") != "CLOSED":
                continue
            try:
                pnl = float(d.get("pnl_lot", 0) or 0)
            except (TypeError, ValueError):
                continue
            values.append((str(row["ts"]), pnl))
        if days > 0 and values:
            latest = max(x[0] for x in values)
            cutoff_day = latest[:10]
            from datetime import date, timedelta
            cutoff = (date.fromisoformat(cutoff_day) - timedelta(days=days - 1)).isoformat()
            values = [x for x in values if x[0][:10] >= cutoff]
        pnls = [p for _, p in values]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        streak = max_streak = 0
        for p in pnls:
            if p < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            elif p > 0:
                streak = 0
        return {"database": path, "closed_signals": len(pnls),
                "win_rate_pct": round(100 * len(wins) / len(pnls), 2) if pnls else None,
                "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
                "avg_loss_abs": round(gross_loss / len(losses), 2) if losses else None,
                "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else ("infinite" if gross_win else None),
                "expectancy_per_trade": round(sum(pnls) / len(pnls), 2) if pnls else None,
                "net_pnl_per_lot": round(sum(pnls), 2), "max_losing_streak": max_streak}
    finally:
        try:
            db.conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline_db")
    ap.add_argument("optimized_db")
    ap.add_argument("--days", type=int, default=30, help="days relative to latest closed signal in each DB; 0 = all")
    args = ap.parse_args()
    print(json.dumps({"baseline": stats(args.baseline_db, args.days),
                      "optimized": stats(args.optimized_db, args.days)}, indent=2))


if __name__ == "__main__":
    main()
