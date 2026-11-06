"""Replay the v2 independent-strategy engine on archived 5m underlying candles.

Examples:
  python tools/replay_v2.py --db "$DATABASE_URL" --start 2026-09-01 --end 2026-10-31
  python tools/replay_v2.py --csv-dir candles/ --symbols NIFTY,RELIANCE --train-frac 0.7 --out v2-replay.csv

This is a closed-bar UNDERLYING replay, not an option-premium backtest. It cannot model
intrabar tick previews, historical option-chain liquidity/Greeks, option spread, theta,
IV changes, brokerage or slippage. Index spot volume is treated as unavailable unless
a reliable futures-volume series is separately supplied. It models the same-candle strategy conflict resolver, without portfolio-wide signal caps.
Stop is assumed first if stop and target are touched in the same candle.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg
from engine.outcomes import summarize
from models import Candle as LegacyCandle
from signal_engine_v2.config import EngineConfig
from signal_engine_v2.models import DecisionState
from signal_engine_v2.runtime import create_engine, evaluate_legacy_candles
from utils.market_hours import IST

BAR = timedelta(minutes=cfg.SCAN_MINUTES)


def load_csv(path: str):
    out = []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            ts = datetime.fromisoformat(row["ts"])
            ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)
            out.append(LegacyCandle(ts, float(row["o"]), float(row["h"]),
                                    float(row["l"]), float(row["c"]),
                                    float(row.get("v") or 0), 0.0))
    return sorted(out, key=lambda x: x.ts)


def by_day(candles):
    result = defaultdict(list)
    for candle in candles:
        result[candle.ts.date()].append(candle)
    return dict(result)


def simulate_outcome(side, entry, stop, target1, target2, future):
    """Conservative underlying R-multiple: stop first, T1 -> breakeven, then T2."""
    sign = 1 if side == "CALL" else -1
    risk = (entry - stop) * sign
    reward1 = (target1 - entry) * sign
    reward2 = (target2 - entry) * sign
    if risk <= 0 or reward1 <= 0 or reward2 <= 0:
        return 0.0, "INVALID_LEVELS", 0, None
    active_stop = stop
    hit_t1 = False
    for index, bar in enumerate(future, 1):
        adverse = bar.l if sign > 0 else bar.h
        favorable = bar.h if sign > 0 else bar.l
        if (adverse - active_stop) * sign <= 0:
            r = (active_stop - entry) * sign / risk
            reason = "BE" if hit_t1 else "SL"
            return r, reason, index, bar.ts + BAR
        if (favorable - target2) * sign >= 0:
            return reward2 / risk, "T2", index, bar.ts + BAR
        if not hit_t1 and (favorable - target1) * sign >= 0:
            hit_t1 = True
            active_stop = entry
        close_time = bar.ts + BAR
        if (close_time.hour, close_time.minute) >= cfg.EXIT_TIME:
            return (bar.c - entry) * sign / risk, "TIME", index, close_time
    if future:
        last = future[-1]
        return (last.c - entry) * sign / risk, "END", len(future), last.ts + BAR
    return 0.0, "END", 0, None


def replay_symbol(name, kind, candles, start, end, engine_config):
    day_map = by_day(candles)
    days = sorted(day_map)
    engine = create_engine(config=engine_config)
    rows = []
    busy_until = -1
    for day_index, day in enumerate(days):
        if day < start or day > end:
            continue
        warm_days = days[max(0, day_index - cfg.HIST_DAYS):day_index]
        warm = [bar for prior_day in warm_days for bar in day_map[prior_day]]
        session = day_map[day]
        busy_until = -1
        for index, bar in enumerate(session):
            close_time = bar.ts + BAR
            hm = (close_time.hour, close_time.minute)
            if index <= busy_until or hm < cfg.FIRST_SIGNAL_TIME or hm > cfg.LAST_SIGNAL_TIME:
                continue
            current_session = session[:index + 1]
            all_bars = warm + current_session
            if len(current_session) < int(cfg.MIN_SESSION_BARS):
                continue
            if len(all_bars) < engine_config.min_history_bars:
                continue
            decisions = evaluate_legacy_candles(
                engine, name, kind, "5m", all_bars,
                session_candles=current_session,
                metadata={"replay": True, "volume_source": "underlying_equity_volume"
                          if kind == "STOCK" else "unavailable"},
            )
            selected = [decision for decision in decisions
                        if decision.state is DecisionState.CONFIRMED]
            if not selected:
                continue
            # Pipeline returns at most one candidate for this underlying/candle.
            candidate = selected[0].candidate
            entry = float(candidate.entry)
            stop = float(candidate.stop)
            target1 = float(candidate.target1)
            target2 = float(candidate.target2 or candidate.target1)
            r, exit_reason, used, exit_ts = simulate_outcome(
                candidate.side.value, entry, stop, target1, target2, session[index + 1:]
            )
            if exit_ts is None:
                exit_ts = close_time
            busy_until = index + max(used, 1)
            grade = str(candidate.metadata.get("grade", "B"))
            rvol = candidate.metadata.get("score_breakdown", {}).get("relative_volume")
            rows.append({
                "symbol": name, "kind": kind, "day": day.isoformat(),
                "signal_ts": close_time.isoformat(timespec="seconds"),
                "exit_ts": exit_ts.isoformat(timespec="seconds") if exit_ts else "",
                "time": close_time.strftime("%H:%M"),
                "tod": f"{close_time.hour:02d}:{(close_time.minute // 15) * 15:02d}",
                "side": candidate.side.value, "strategy": candidate.strategy,
                "score": round(float(candidate.score), 2), "grade": grade,
                "entry": entry, "stop": stop, "target1": target1, "target2": target2,
                "r": round(float(r), 3), "exit": exit_reason, "bars": used,
                "htf_bias": candidate.metadata.get("htf_bias", ""),
                "volume_score": rvol if rvol is not None else "",
            })
    return rows



def report(title, rows, key):
    buckets = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(float(row["r"]))
    print(f"\n{title}")
    print(f"  {'bucket':<26}{'n':>6}{'win%':>7}{'avgR':>8}{'PF':>7}{'totalR':>9}")
    for name, values in sorted(buckets.items()):
        stats = summarize(values)
        print(f"  {str(name):<26}{stats['n']:>6}{stats['win_pct']:>7}{stats['avg_r']:>8}"
              f"{stats['pf']:>7}{stats['total_r']:>9}")


def print_report(rows, label=""):
    if not rows:
        print(f"{label}No qualifying closed-bar signals.")
        return
    stats = summarize([float(row["r"]) for row in rows])
    print(f"\n=== {label} n={stats['n']} win%={stats['win_pct']} avgR={stats['avg_r']} "
          f"PF={stats['pf']} totalR={stats['total_r']} max losing streak={stats['max_lose_streak']}")
    report("By strategy", rows, lambda x: x["strategy"])
    report("By index/stock", rows, lambda x: x["kind"])
    report("By grade", rows, lambda x: x["grade"])
    report("By time of day", rows, lambda x: x["tod"])
    report("By exit", rows, lambda x: x["exit"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db")
    source.add_argument("--csv-dir")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default="2100-01-01")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--indices", default=",".join(cfg.INDEX_NAMES))
    parser.add_argument("--min-score", type=float, default=float(cfg.V2_MIN_SCORE))
    parser.add_argument("--min-rr", type=float, default=float(cfg.V2_MIN_RR))
    parser.add_argument("--train-frac", type=float, default=0.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wanted = {x.strip().upper() for x in args.symbols.split(",") if x.strip()}
    index_names = {x.strip().upper() for x in args.indices.split(",") if x.strip()}
    sources = {}
    if args.db:
        from db import DB
        db = DB(args.db)
        for symbol in db.candle_symbols():
            if not wanted or symbol.upper() in wanted:
                sources[symbol] = db.candles_between(symbol, "2000-01-01", "2100-01-01")
    else:
        for filename in sorted(os.listdir(args.csv_dir)):
            if filename.lower().endswith(".csv") and (not wanted or filename[:-4].upper() in wanted):
                sources[filename[:-4]] = load_csv(os.path.join(args.csv_dir, filename))
    if not sources:
        print("No candle data found. Enable CANDLE_ARCHIVE_ENABLED or supply CSV files.")
        return 1

    engine_config = EngineConfig(
        min_score=float(args.min_score), min_risk_reward=float(args.min_rr),
        min_history_bars=max(220, int(cfg.V2_MIN_HISTORY_BARS)),
        allow_intrabar_candidates=False,
    )
    raw_rows = []
    for symbol, candles in sources.items():
        kind = "INDEX" if symbol.upper() in index_names else "STOCK"
        raw_rows.extend(replay_symbol(symbol, kind, candles, start, end, engine_config))
    rows = raw_rows
    print(f"Qualifying strategy candidates (no portfolio caps): {len(rows)}")

    if args.train_frac and rows and not 0.0 < args.train_frac < 1.0:
        parser.error("--train-frac must be greater than 0 and less than 1")
    if args.train_frac and rows and len({row["day"] for row in rows}) > 1:
        days = sorted({row["day"] for row in rows})
        cut_index = max(1, min(len(days) - 1, int(len(days) * args.train_frac)))
        cutoff = days[cut_index]
        print_report([row for row in rows if row["day"] < cutoff], f"TRAIN (< {cutoff})")
        print_report([row for row in rows if row["day"] >= cutoff], f"TEST (>= {cutoff})")
    else:
        print_report(rows)

    if args.out and rows:
        with open(args.out, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
