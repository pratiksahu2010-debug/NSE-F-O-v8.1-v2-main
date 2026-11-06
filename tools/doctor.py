"""Pipeline doctor: finds the stage where signals are being lost, using your real Upstox / Telegram credentials.

    python tools/doctor.py                 # token, instrument master, candles, rule, option chain (30 symbols)
    python tools/doctor.py --symbols 106   # whole universe (slower: Upstox is paced by UPSTOX_RPS)
    python tools/doctor.py --telegram      # also send one test message to every TELEGRAM_CHAT_IDS entry
    python tools/doctor.py --selftest      # offline: run the checks against synthetic data (no credentials)

It never places orders and never writes to the database. Run it on the Render shell, or locally with the same
environment variables (UPSTOX_ANALYTICS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS).
Use it during market hours (09:20-15:25 IST) so that today's candles exist; outside hours it still checks the rest.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg  # noqa: E402
from engine.chain_quality import chain_quality_issues  # noqa: E402
from engine.contract_picker import pick_contract, pick_expiry  # noqa: E402
from engine.risk_rules import RiskRules  # noqa: E402
from indicators.core import atr  # noqa: E402
from strategies.simple_signal import evaluate_simple  # noqa: E402
from utils.market_hours import IST, can_signal, is_trading_day  # noqa: E402

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


class Report:
    def __init__(self, out=print):
        self.out, self.counts = out, Counter()

    def line(self, level: str, title: str, detail: str = ""):
        self.counts[level] += 1
        self.out(f"[{level}] {title}" + (f" - {detail}" if detail else ""))

    def info(self, text: str = ""):
        self.out(text)


def _closed_session(raw, today, now):
    seen = {}
    for c in raw or []:
        ts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=IST)
        if ts.date() == today and ts + timedelta(minutes=cfg.SCAN_MINUTES) <= now:
            seen[ts] = c
    return [seen[k] for k in sorted(seen)]


def check_environment(rep: Report):
    rep.info("== 1. Environment")
    has_token = any([cfg.UPSTOX_ANALYTICS_TOKEN, cfg.UPSTOX_ACCESS_TOKEN, cfg.UPSTOX_TOKEN_FILE])
    rep.line(OK if has_token else FAIL, "Upstox token configured",
             "" if has_token else "set UPSTOX_ANALYTICS_TOKEN (Upstox Developer Apps -> Analytics -> Generate)")
    tg_ok = bool(cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_IDS)
    rep.line(OK if tg_ok else FAIL, "Telegram token and chat IDs configured",
             "" if tg_ok else "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS missing: signals would be generated but never delivered")
    db_ok = bool(cfg.DATABASE_URL)
    rep.line(OK if db_ok else WARN, "DATABASE_URL configured",
             "" if db_ok else "the bot refuses to start without DATABASE_URL (PostgreSQL)")
    rep.line(OK, f"Strategy mode {cfg.STRATEGY_MODE}",
             f"ADX>={cfg.SIMPLE_ADX_MIN:g}, RSI call {cfg.SIMPLE_RSI_CALL_MIN:g}-{cfg.SIMPLE_RSI_CALL_MAX:g}, "
             f"put {cfg.SIMPLE_RSI_PUT_MIN:g}-{cfg.SIMPLE_RSI_PUT_MAX:g}, fresh<={cfg.SIMPLE_FRESH_BARS} candles")
    now = datetime.now(IST)
    rep.line(OK if is_trading_day(now.date()) else WARN, f"Clock {now:%a %d-%b %H:%M:%S} IST",
             "trading day" if is_trading_day(now.date()) else "weekend or listed holiday: no scans run")
    rep.line(OK if can_signal(now) else WARN, "Inside the signal window "
             f"{cfg.FIRST_SIGNAL_TIME[0]:02d}:{cfg.FIRST_SIGNAL_TIME[1]:02d}-{cfg.LAST_SIGNAL_TIME[0]:02d}:{cfg.LAST_SIGNAL_TIME[1]:02d}",
             "" if can_signal(now) else "the scanner is idle right now; candle checks below use the last session")


def check_token(rep: Report, fo):
    rep.info("== 2. Upstox token")
    try:
        data = fo.call("ping")
        rep.line(OK if data else FAIL, "Upstox accepts the token (quote endpoint)")
        return bool(data)
    except Exception as exc:
        rep.line(FAIL, "Upstox rejected the request", f"{type(exc).__name__}: {exc}")
        return False


def check_universe(rep: Report, master, unds):
    rep.info("== 3. Instrument master / universe")
    from strategies.mex_universe import MEX_SYMBOLS
    from strategies.normal_universe import NORMAL_SYMBOLS
    names = {u.name for u in unds}
    rep.line(OK if unds else FAIL, f"{len(unds)} underlyings resolved",
             f"{sum(1 for u in unds if u.kind == 'INDEX')} indices, {sum(1 for u in unds if u.kind == 'STOCK')} stocks")
    missing = [n for n in dict.fromkeys(list(NORMAL_SYMBOLS) + list(MEX_SYMBOLS)) if n not in names]
    rep.line(OK if len(missing) <= 8 else WARN, f"{len(missing)} fixed-list symbols missing from the F&O master",
             ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "") if missing else "")
    return missing


def check_candles(rep: Report, fo, unds, today, now, limit):
    rep.info("== 4. Candles and the signal rule")
    sample = unds[:limit]
    expected_open = None
    if can_signal(now):
        step = cfg.SCAN_MINUTES
        boundary = now.replace(minute=now.minute - now.minute % step, second=0, microsecond=0)
        expected_open = boundary - timedelta(minutes=step)
    tally, gates, errors, triggers = Counter(), Counter(), Counter(), []
    lags = []
    for u in sample:
        try:
            raw = fo.call("spot_intraday", u)
        except Exception as exc:
            errors[f"{type(exc).__name__}: {exc}"[:90]] += 1
            tally["fetch_failed"] += 1
            continue
        session = _closed_session(raw, today, now)
        if not session:
            tally["no_closed_candles_today"] += 1
            continue
        lag = (now - (session[-1].ts + timedelta(minutes=cfg.SCAN_MINUTES))).total_seconds()
        lags.append(lag)
        if expected_open is not None and session[-1].ts < expected_open:
            tally["latest_closed_candle_missing"] += 1
        try:
            hist = fo.call("history", u, cfg.HIST_DAYS)
        except Exception as exc:
            errors[f"history: {type(exc).__name__}: {exc}"[:90]] += 1
            hist = []
        if len(hist) + len(session) < cfg.SIMPLE_MIN_BARS:
            tally["history_too_short"] += 1
        allc = sorted({c.ts: c for c in list(hist) + session}.values(), key=lambda c: c.ts)
        result = evaluate_simple(session, allc, adx_min=cfg.SIMPLE_ADX_MIN, rsi_call_min=cfg.SIMPLE_RSI_CALL_MIN,
                                 rsi_call_max=cfg.SIMPLE_RSI_CALL_MAX, rsi_put_max=cfg.SIMPLE_RSI_PUT_MAX,
                                 rsi_put_min=cfg.SIMPLE_RSI_PUT_MIN, fresh_bars=cfg.SIMPLE_FRESH_BARS,
                                 min_bars=cfg.SIMPLE_MIN_BARS, min_session_bars=cfg.SIMPLE_MIN_SESSION_BARS)
        tally["evaluated"] += 1
        if result["side"]:
            triggers.append((u, result))
        else:
            for g in result.get("gate_failures", [])[:2]:
                gates[g] += 1
    n = len(sample)
    rep.line(OK if tally["evaluated"] >= max(1, n // 2) else FAIL,
             f"{tally['evaluated']}/{n} symbols returned usable candles",
             f"failed {tally['fetch_failed']}, no closed candle today {tally['no_closed_candles_today']}")
    for msg, cnt in errors.most_common(3):
        rep.line(FAIL, f"provider error x{cnt}", msg)
    if lags:
        worst = max(lags)
        rep.line(OK if worst <= cfg.MAX_CANDLE_LAG_SEC else WARN,
                 f"latest candle lag: median {sorted(lags)[len(lags) // 2]:.0f}s, worst {worst:.0f}s",
                 f"scanner discards symbols above {cfg.MAX_CANDLE_LAG_SEC}s")
    if tally["latest_closed_candle_missing"]:
        rep.line(WARN, f"{tally['latest_closed_candle_missing']} symbols did not have the just-closed candle yet",
                 f"V8.1 waits up to {cfg.CANDLE_READY_RETRIES} x {cfg.CANDLE_READY_WAIT_SEC:g}s for it")
    if tally["history_too_short"]:
        rep.line(WARN, f"{tally['history_too_short']} symbols have fewer than {cfg.SIMPLE_MIN_BARS} candles of history",
                 "V8.1 backfills history during the scan; persistent shortage means the history endpoint is failing")
    rep.line(OK if triggers else WARN, f"rule triggers right now: {len(triggers)}",
             "" if triggers else "no symbol currently satisfies EMA+VWAP+RSI+ADX; blockers below")
    if gates and not triggers:
        rep.info("    most common blockers: " + ", ".join(f"{g} x{c}" for g, c in gates.most_common(6)))
    for u, r in triggers[:5]:
        rep.info(f"    TRIGGER {u.name} {'CALL' if r['side'] > 0 else 'PUT'}: {r['reason'][:150]}")
    return triggers, sample


def check_chain(rep: Report, fo, master, unds, triggers, today):
    rep.info("== 5. Option chain, contract and risk levels")
    targets = [(u, "CALL" if r["side"] > 0 else "PUT") for u, r in triggers[:3]]
    if not targets:
        nifty = next((u for u in unds if u.name == "NIFTY"), unds[0] if unds else None)
        targets = [(nifty, "CALL")] if nifty else []
        rep.info("    no live trigger: dry-running the chain/contract/risk path on " + (nifty.name if nifty else "n/a"))
    for u, side in targets:
        expiry = pick_expiry(master, u, today)
        if not expiry:
            rep.line(FAIL, f"{u.name}: no eligible expiry in the master")
            continue
        try:
            chain = fo.call("option_chain", u, expiry)
        except Exception as exc:
            rep.line(FAIL, f"{u.name}: option chain request failed", f"{type(exc).__name__}: {exc}")
            continue
        issues = chain_quality_issues(chain, chain.spot, side=side)
        rep.line(OK if not issues else WARN, f"{u.name}: chain {expiry} has {len(chain.rows)} quotes, spot {chain.spot:g}",
                 "; ".join(issues))
        contract, q, why = pick_contract(master, u, side, chain, datetime.now(IST))
        if not contract:
            rep.line(FAIL, f"{u.name} {side}: no tradable contract", why)
            continue
        ask = float(getattr(q, "ask", 0.0) or 0.0)
        rep.line(OK if ask > 0 else FAIL, f"{u.name} {side}: {contract.trading_symbol}", f"ask {ask:g}, delta {q.delta:g}")
        try:
            a5 = float(atr(fo.call("spot_intraday", u), 14)[-1])
        except Exception:
            a5 = 0.0
        lv = RiskRules.levels(ask, q.delta, a5, contract.tick_size, side, chain.spot) if ask > 0 and a5 > 0 else None
        rep.line(OK if lv else FAIL, f"{u.name} {side}: risk levels",
                 f"SL {lv['sl']:g} T1 {lv['t1']:g} T2 {lv['t2']:g} RR {lv['rr']:.2f}" if lv else
                 "rejected (premium too small for the ATR stop, or ATR unavailable)")


def check_telegram(rep: Report, send: bool):
    rep.info("== 6. Telegram")
    from telegram_service import Telegram
    tg = Telegram()
    if not (tg.token and tg.chat_ids):
        rep.line(FAIL, "Telegram not configured")
        return
    if not send:
        rep.line(WARN, "delivery not tested", "re-run with --telegram to send a test message")
        return
    ok = tg.send("✅ Doctor test message: Telegram delivery works.")
    rep.line(OK if ok else FAIL, "test message delivered to every configured chat", "" if ok else str(tg.last_error))


def run(fo, master, unds, *, limit=30, telegram=False, out=print, today=None, now=None, run_env=True, run_tg=True):
    rep = Report(out)
    now = now or datetime.now(IST)
    today = today or now.date()
    if run_env:
        check_environment(rep)
    if check_token(rep, fo) is False:
        rep.info("Fix the token first: every later check depends on it.")
    check_universe(rep, master, unds)
    triggers, _ = check_candles(rep, fo, unds, today, now, limit)
    check_chain(rep, fo, master, unds, triggers, today)
    if run_tg:
        check_telegram(rep, telegram)
    rep.info("")
    rep.info(f"Summary: {rep.counts[OK]} pass, {rep.counts[WARN]} warn, {rep.counts[FAIL]} fail")
    return rep


def _live(args):
    from data.instruments import Master, build_underlyings, download_master
    from data.upstox_client import UpstoxProvider
    from data.upstox_only import UpstoxOnlyProvider
    fo = UpstoxOnlyProvider(UpstoxProvider())
    t0 = time.monotonic()
    master = Master(download_master())
    unds = build_underlyings(master, datetime.now(IST).date())
    print(f"(master loaded in {time.monotonic() - t0:.1f}s)")
    # Indices first, then the liquid fixed list, so a small --symbols value still covers the main names.
    unds.sort(key=lambda u: (u.kind != "INDEX", u.name))
    return run(fo, master, unds, limit=args.symbols, telegram=args.telegram)


def _selftest(args):
    """Offline run against the repo's synthetic fixtures: proves the doctor itself works."""
    from tests.test_core import TODAY, master_rows  # noqa: WPS433
    from tests.test_scanner_e2e import FIXTURE_NOW, FakeProvider  # noqa: WPS433
    from tests.test_scanner_e2e import build  # noqa: WPS433
    from data.instruments import Master, build_underlyings

    sc, _tg, _tracker, now, sess = build(1)

    class Fo:
        active_name = "upstox"

        def call(self, method, *a, **k):
            if method == "ping":
                return True
            if method == "history":
                return sc.hist["NIFTY"]
            return getattr(FakeProvider(sc.hist["NIFTY"], sess, sess, 0.0, 1), method)(*a, **k)

    master = Master(master_rows())
    unds = [u for u in build_underlyings(master, TODAY) if u.name == "NIFTY"]
    return run(Fo(), master, unds, limit=5, now=now, today=TODAY, run_env=False, run_tg=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", type=int, default=30, help="how many underlyings to test (default 30)")
    ap.add_argument("--telegram", action="store_true", help="send a real test message")
    ap.add_argument("--selftest", action="store_true", help="offline run on synthetic data")
    args = ap.parse_args(argv)
    rep = _selftest(args) if args.selftest else _live(args)
    return 1 if rep.counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
