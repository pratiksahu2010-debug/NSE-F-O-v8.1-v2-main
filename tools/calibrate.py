"""Calibrate the live gates from the bot's OWN tracked outcomes (premium reality: ask entry, bid exit, costs).

  python tools/calibrate.py            # last 45 days, uses DATABASE_URL like the bot
  python tools/calibrate.py 90 --db postgresql://user:pw@host/db

Reads closed signals + the per-signal features the scanner logs (score, components, ADX/RSI, context gates...),
converts P&L to R-multiples, and shows win rate / average R / profit factor per score band, strategy|kind, time of
day, relative strength, time-of-day volume, 15m alignment and DTE. It then prints tighten-only suggestions.
It never edits config: the numbers are evidence for you to act on, and need a real sample (>= 40 per bucket).
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg  # noqa: E402
from engine.adaptive import tod_bucket  # noqa: E402
from engine.outcomes import r_multiple, summarize  # noqa: E402

MIN_N = 20


def _bucket_num(v, edges, fmt="{:g}"):
    if v is None:
        return "n/a"
    lo = None
    for e in edges:
        if v < e:
            return f"<{fmt.format(e)}" if lo is None else f"{fmt.format(lo)}-{fmt.format(e)}"
        lo = e
    return f">={fmt.format(edges[-1])}"


def collect(db, days: int):
    start = (date.today() - timedelta(days=days)).isoformat()
    feats = db.features_since(start)
    out = []
    for s in db.closed_since(start):
        f = feats.get(s.id, {})
        out.append({"r": r_multiple(s), "sig": s, "f": f})
    return out


def tables(rows):
    defs = {
        "Score band": lambda x: f"{int(x['f']['score'] * 2) / 2:.1f}+" if x["f"].get("score") is not None else "n/a",
        "Strategy|kind": lambda x: f"{x['sig'].strategy}|{x['sig'].kind or '?'}",
        "Time of day": lambda x: tod_bucket(x["sig"].ts) or "n/a",
        "15m trend vs side": lambda x: ("aligned" if x["f"].get("htf") == x["f"].get("side_sign") else
                                         "opposed" if x["f"].get("htf") == -(x["f"].get("side_sign") or 9) else "mixed")
        if x["f"] else "n/a",
        "Rel. strength vs NIFTY (stocks, %)": lambda x: _bucket_num(
            None if x["f"].get("rs_pct") is None else x["f"]["rs_pct"] * x["f"].get("side_sign", 1), [-0.3, 0, 0.3, 0.8], "{:+g}"),
        "Same-time volume ratio": lambda x: _bucket_num(x["f"].get("tod_vol_ratio"), [0.8, 1.2, 2.0]),
        "ADX": lambda x: _bucket_num(x["f"].get("adx"), [18, 25, 35]),
        "DTE": lambda x: _bucket_num(x["f"].get("dte"), [2, 4, 8]),
        "Spread %": lambda x: _bucket_num(x["f"].get("spread_pct"), [1, 2, 3, 4]),
    }
    res = {}
    for title, key in defs.items():
        g = defaultdict(list)
        for x in rows:
            g[key(x)].append(x["r"])
        res[title] = {k: summarize(v) for k, v in sorted(g.items())}
    return res


def suggestions(rows, res):
    out = []
    sk = res.get("Strategy|kind", {})
    for k, s in sk.items():
        if s["n"] >= max(MIN_N, cfg.ADAPT_MIN_N // 2) and s["avg_r"] < 0:
            out.append(f"{k}: avg R {s['avg_r']} over {s['n']} signals is negative -> disable it (e.g. {k.split('|')[0]}_ENABLED=false) or raise its bar.")
    scored = sorted([x for x in rows if x["f"].get("score") is not None], key=lambda x: x["f"]["score"])
    best = None
    for i in range(len(scored)):
        tail = [x["r"] for x in scored[i:]]
        if len(tail) >= 30 and sum(tail) / len(tail) > 0 and (best is None or scored[i]["f"]["score"] < best[0]):
            best = (scored[i]["f"]["score"], len(tail), sum(tail) / len(tail))
    if best:
        out.append(f"ALERT_SCORE_MIN: signals scoring >= {best[0]:.1f} have avg R {best[2]:+.2f} over {best[1]} signals "
                   f"(live floor is {cfg.ALERT_SCORE_MIN:.1f}); only consider RAISING it above that if the lower bands are negative.")
    else:
        out.append("ALERT_SCORE_MIN: not enough signals (>= 30 with positive tail expectancy) to say anything yet.")
    opp = res.get("15m trend vs side", {}).get("opposed")
    if opp and opp["n"] >= MIN_N and opp["avg_r"] < 0:
        out.append(f"15m opposed signals still lose (avg R {opp['avg_r']}, n={opp['n']}): keep HTF_VETO_ENABLED=true.")
    sp = res.get("Spread %", {})
    wide = [s for k, s in sp.items() if k.startswith(("3-", "4-", ">=")) and s["n"] >= MIN_N and s["avg_r"] < 0]
    if wide:
        out.append("Wide-spread signals (3%+) have negative avg R: consider lowering MAX_SPREAD_PCT / MAX_SPREAD_PCT_STOCK.")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("days", nargs="?", type=int, default=cfg.ADAPT_DAYS)
    ap.add_argument("--db", default=None, help="PostgreSQL URL (default: DATABASE_URL like the bot)")
    a = ap.parse_args(argv)
    from db import DB
    db = DB(a.db) if a.db else DB()
    rows = collect(db, a.days)
    if not rows:
        print("no closed signals in the window")
        return 1
    s = summarize([x["r"] for x in rows])
    print(f"Closed signals (last {a.days} days): n={s['n']} win%={s['win_pct']} avgR={s['avg_r']} PF={s['pf']} totalR={s['total_r']} "
          f"max losing streak={s['max_lose_streak']}")
    missing = sum(1 for x in rows if not x["f"])
    if missing:
        print(f"({missing} signals predate feature logging and appear only in strategy/time tables)")
    res = tables(rows)
    for title, g in res.items():
        print(f"\n{title}")
        print(f"  {'bucket':<22}{'n':>5}{'win%':>7}{'avgR':>8}{'PF':>7}{'totalR':>8}")
        for k, v in g.items():
            print(f"  {k:<22}{v['n']:>5}{v['win_pct']:>7}{v['avg_r']:>8}{v['pf']:>7}{v['total_r']:>8}")
    print("\nSuggestions (evidence, not orders):")
    for line in suggestions(rows, res):
        print(" -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
