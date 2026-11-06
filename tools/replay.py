"""Candle-level replay: run the live signal logic over archived 5m candles and score every signal by what the
UNDERLYING did afterwards. This is the tool for deciding which setups, score bands and gates actually work.

  python tools/replay.py --db "$DATABASE_URL" --start 2026-10-01 --end 2026-10-29
  python tools/replay.py --csv-dir candles/ --start 2026-10-01 --train-frac 0.7 --out replay.csv
  python tools/replay.py --db "$DATABASE_URL" --no-context-gates        # A/B: what do the context gates add?
  RESEARCH_MODE=true python tools/replay.py --db "$DATABASE_URL" --min-score 6.5   # study bands below the live 8.0 floor

Data: the live bot archives every closed session candle to the `candles` table (CANDLE_ARCHIVE_ENABLED), or supply
CSV files named <SYMBOL>.csv with columns ts,o,h,l,c,v (ISO timestamps, IST). Earlier days of the same file are the
indicator warm-up, so the first days of a fresh archive only provide warm-up.

Honest limits (read before trusting numbers):
  * Outcomes are on the UNDERLYING with the same stop/target geometry as the live risk rules, not option premium:
    no theta, IV change, spread or brokerage. Use tools/calibrate.py on live-tracked signals for premium reality.
  * Within a candle where stop and target are both touched, the stop is assumed first (conservative).
  * NIFTY-relative gates, OI wall and futures OI need data the archive does not have; those gates are skipped here.
  * Entry is the signal candle's close. A few weeks of data is a sanity check, not proof. Prefer --train-frac so
    parameters are judged on days they were not tuned on.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg  # noqa: E402
from engine import context as ctxmod, scorer  # noqa: E402
from engine.outcomes import summarize  # noqa: E402
from indicators.core import atr, htf_bias, htf_vwap_bias  # noqa: E402
from models import Candle  # noqa: E402
from strategies.price import invalidation_distance, price_votes, strategy_of  # noqa: E402
from strategies.regime import regime as calc_regime  # noqa: E402
from utils.market_hours import IST  # noqa: E402

BAR = timedelta(minutes=cfg.SCAN_MINUTES)


def load_csv(path: str):
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r["ts"])
            ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)
            out.append(Candle(ts, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r.get("v") or 0)))
    return sorted(out, key=lambda c: c.ts)


def by_day(candles):
    d = defaultdict(list)
    for c in candles:
        d[c.ts.date()].append(c)
    return dict(d)


def outcome(sgn: int, entry: float, sl_d: float, t1_d: float, t2_d: float, bars, exit_hm=None):
    """Walk forward bar by bar mirroring the tracker: stop, T1 moves the stop to entry, the halfway point between T1
    and T2 trails the stop to T1, T2 closes the trade, hard time exit at EXIT_TIME. Returns (R, reason, bars_used)."""
    exit_hm = exit_hm or cfg.EXIT_TIME
    stop = entry - sgn * sl_d
    t1, t2 = entry + sgn * t1_d, entry + sgn * t2_d
    trail = entry + sgn * (t1_d + 0.5 * (t2_d - t1_d))
    hit1 = trailed = False
    for k, b in enumerate(bars, 1):
        adverse, favor = (b.l, b.h) if sgn > 0 else (b.h, b.l)
        if sgn * (adverse - stop) <= 0:                                   # stop first when both are touched
            r = sgn * (stop - entry) / sl_d
            return r, ("TRAIL_T1" if trailed else "BE" if hit1 else "SL"), k
        if sgn * (favor - t2) >= 0:
            return t2_d / sl_d, "T2", k
        if not hit1 and sgn * (favor - t1) >= 0:
            hit1, stop = True, entry
        if hit1 and not trailed and sgn * (favor - trail) >= 0:
            trailed, stop = True, t1
        close_t = b.ts + BAR
        if (close_t.hour, close_t.minute) >= exit_hm:
            return sgn * (b.c - entry) / sl_d, "TIME", k
    last = bars[-1] if bars else None
    return (sgn * (last.c - entry) / sl_d if last else 0.0), "END", len(bars)


def replay_symbol(name, kind, candles, start, end, min_score, context_gates=True):
    days = sorted(by_day(candles))
    dmap = by_day(candles)
    rows = []
    for di, d in enumerate(days):
        if d < start or d > end:
            continue
        warm = [c for dd in days[max(0, di - cfg.HIST_DAYS):di] for c in dmap[dd]]
        prev_close = dmap[days[di - 1]][-1].c if di > 0 else None
        day_c = dmap[d]
        busy_until = -1
        for i in range(len(day_c)):
            close_t = day_c[i].ts + BAR
            hm = (close_t.hour, close_t.minute)
            if i <= busy_until or hm < cfg.FIRST_SIGNAL_TIME or hm > cfg.LAST_SIGNAL_TIME:
                continue
            session, allc = day_c[:i + 1], warm + day_c[:i + 1]
            if len(session) < cfg.MIN_SESSION_BARS or len(allc) < cfg.MIN_BARS:
                continue
            votes = price_votes(session, allc)
            if not votes or votes[0].score == 0:
                continue
            side = "CALL" if votes[0].score > 0 else "PUT"
            sgn = 1 if side == "CALL" else -1
            htf = htf_bias(allc, cfg.HTF_MINUTES)
            try:
                reg = calc_regime(session, prev_close, [], allc, d, False)
            except Exception:
                reg = {}
            gate = scorer.score_10_point(side, session, allc, reg, htf=htf, mkt=0, votes=votes,
                                         htf_vwap=htf_vwap_bias(session, cfg.HTF_MINUTES))
            if gate["rejects"] or gate["score"] < min_score:
                continue
            a = atr(allc, 14)[-1]
            entry = day_c[i].c
            is_rev = votes[0].name == "REVERSAL"
            feats = ctxmod.compute_features(kind, side, allc, session, None, a, entry, htf, 0, None)
            veto = ctxmod.veto_from(feats, kind, is_rev) if context_gates else None
            if veto:
                continue
            strategy = strategy_of(votes[0].name)
            stop_dist = invalidation_distance(side, session, allc, entry, strategy)
            sl_d = min(max(stop_dist, cfg.SL_MIN_ATR * a), cfg.SL_MAX_ATR * a)
            t1_d = max(cfg.T1_ATR * a, cfg.T1_R * sl_d)
            t2_d = max(cfg.T2_ATR * a, cfg.T2_R * sl_d)
            r, why, used = outcome(sgn, entry, sl_d, t1_d, t2_d, day_c[i + 1:])
            busy_until = i + max(used, 1)                                  # one open trade per symbol at a time
            step = max(5, cfg.ADAPT_TOD_MINUTES)
            rows.append({"symbol": name, "kind": kind, "day": d.isoformat(), "time": f"{hm[0]:02d}:{hm[1]:02d}",
                         "tod": f"{hm[0]:02d}:{(hm[1] // step) * step:02d}", "side": side, "strategy": strategy,
                         "score": gate["score"], "r": round(r, 3), "exit": why, "bars": used})
    return rows


def _table(title, rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r["r"])
    print(f"\n{title}")
    print(f"  {'bucket':<22}{'n':>5}{'win%':>7}{'avgR':>8}{'PF':>7}{'totalR':>8}{'maxLS':>6}")
    for k in sorted(g):
        s = summarize(g[k])
        print(f"  {str(k):<22}{s['n']:>5}{s['win_pct']:>7}{s['avg_r']:>8}{s['pf']:>7}{s['total_r']:>8}{s['max_lose_streak']:>6}")


def report(rows, label=""):
    if not rows:
        print(f"{label}no signals")
        return
    s = summarize([r["r"] for r in rows])
    print(f"\n=== {label}overall: n={s['n']} win%={s['win_pct']} avgR={s['avg_r']} PF={s['pf']} totalR={s['total_r']}")
    _table("By strategy", rows, lambda r: r["strategy"])
    _table("By kind", rows, lambda r: r["kind"])
    _table("By strategy|kind", rows, lambda r: f"{r['strategy']}|{r['kind']}")
    _table("By score band", rows, lambda r: f"{int(r['score'] * 2) / 2:.1f}+")
    _table("By time of day", rows, lambda r: r["tod"])
    _table("By exit", rows, lambda r: r["exit"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db")
    src.add_argument("--csv-dir")
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--end", default="2100-01-01")
    ap.add_argument("--symbols", default="", help="comma list; default all")
    ap.add_argument("--indices", default=",".join(cfg.INDEX_NAMES), help="symbols treated as INDEX kind")
    ap.add_argument("--min-score", type=float, default=cfg.ALERT_SCORE_MIN)
    ap.add_argument("--no-context-gates", action="store_true")
    ap.add_argument("--train-frac", type=float, default=0.0, help="e.g. 0.7: report first 70%% of days separately from the last 30%%")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    indices = {x.strip().upper() for x in a.indices.split(",") if x.strip()}
    want = {x.strip().upper() for x in a.symbols.split(",") if x.strip()}
    sources = {}
    if a.db:
        from db import DB
        db = DB(a.db)
        for sym in db.candle_symbols():
            if not want or sym.upper() in want:
                sources[sym] = db.candles_between(sym, "2000-01-01", "2100-01-01")
    else:
        for fn in sorted(os.listdir(a.csv_dir)):
            if fn.lower().endswith(".csv") and (not want or fn[:-4].upper() in want):
                sources[fn[:-4]] = load_csv(os.path.join(a.csv_dir, fn))
    if not sources:
        print("no candle data found (is the bot archiving? CANDLE_ARCHIVE_ENABLED=true)")
        return 1
    rows = []
    for sym, candles in sources.items():
        kind = "INDEX" if sym.upper() in indices else "STOCK"
        rows += replay_symbol(sym, kind, candles, start, end, a.min_score, not a.no_context_gates)
    rows.sort(key=lambda r: (r["day"], r["time"]))
    if a.train_frac and rows:
        days = sorted({r["day"] for r in rows})
        cut = days[max(0, min(len(days) - 1, int(len(days) * a.train_frac)))]
        report([r for r in rows if r["day"] < cut], f"TRAIN (< {cut}) ")
        report([r for r in rows if r["day"] >= cut], f"TEST  (>= {cut}) ")
    else:
        report(rows)
    if a.out and rows:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {len(rows)} signals to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
