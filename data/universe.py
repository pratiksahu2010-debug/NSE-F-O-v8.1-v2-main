"""Liquidity-ranked F&O universe with bounded concurrent requests and startup progress logs."""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import List

from config import cfg
from engine.analytics import rerank_due
from models import Underlying

log = logging.getLogger("universe")


def _cache_path():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    return os.path.join(cfg.DATA_DIR, "top_stocks.json")


def load_cached(today: date, db=None):
    try:
        raw = db.kv_get("top_stocks") if db is not None else None
        if raw:
            d = json.loads(raw)
        else:
            with open(_cache_path(), encoding="utf-8") as f:
                d = json.load(f)
        age = (today - date.fromisoformat(d["date"])).days
        if rerank_due(d.get("ts", 0), time.time(), cfg.UNIVERSE_RERANK_HOURS):
            log.info("Universe cache stale | cached=%s | re-ranking (limit %.1fh)", d.get("date"), cfg.UNIVERSE_RERANK_HOURS)
            return None
        if 0 <= age <= cfg.UNIVERSE_CACHE_DAYS and d.get("n") == cfg.TOP_STOCKS:
            stocks = d.get("stocks")
            if isinstance(stocks, list) and stocks:
                log.info("Universe cache hit | date=%s | symbols=%d", d["date"], len(stocks))
                return stocks
    except Exception:
        pass
    log.info("Universe cache miss | ranking daily turnover")
    return None


def _score_one(provider, u):
    """Return (turnover, symbol); provider errors are isolated per symbol."""
    try:
        candles = provider.daily(u, 30)[-20:]
        if len(candles) < 10:
            return None, u.name, "insufficient_daily_candles"
        turnover = sum(float(c.c) * float(c.v) for c in candles) / len(candles)
        if turnover <= 0:
            return None, u.name, "nonpositive_turnover"
        return turnover, u.name, None
    except Exception as exc:
        return None, u.name, f"{type(exc).__name__}: {exc}"


def rank_stocks(provider, stocks: List[Underlying], top_n: int, today: date, db=None) -> List[str]:
    """Rank by average daily turnover (close*volume), with bounded concurrency."""
    started = time.monotonic()
    count = len(stocks)
    if top_n <= 0 or not stocks:
        log.warning("Liquidity ranking skipped | candidates=%d | top_n=%d", count, top_n)
        return []

    # Keep concurrency bounded to avoid overwhelming provider rate limits.
    workers = max(1, min(4, int(getattr(cfg, "SCAN_WORKERS", 4) or 4), count))
    log.info("Liquidity ranking started | candidates=%d | workers=%d | top_n=%d", count, workers, top_n)
    scored = []
    failures = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="universe-rank") as pool:
        futures = {pool.submit(_score_one, provider, u): u.name for u in stocks}
        for future in as_completed(futures):
            completed += 1
            try:
                turnover, name, error = future.result()
            except Exception as exc:
                turnover, name, error = None, futures[future], f"{type(exc).__name__}: {exc}"
            if turnover is not None:
                scored.append((turnover, name))
            else:
                failures += 1
                # Warn on individual failures without hiding ranking progress.
                if error and completed <= 5:
                    log.warning("Liquidity ranking unavailable | symbol=%s | reason=%s", name, error)
            if completed % 5 == 0 or completed == count:
                log.info("Liquidity ranking progress | completed=%d/%d | ranked=%d | failed_or_thin=%d | elapsed=%.1fs",
                         completed, count, len(scored), failures, time.monotonic() - started)

    scored.sort(reverse=True)
    names = [name for _, name in scored[:top_n]]
    if names:
        payload = {"date": today.isoformat(), "n": top_n, "stocks": names, "ts": time.time()}
        try:
            path = _cache_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not write universe cache: %s", exc)
        if db is not None:
            try:
                db.kv_set("top_stocks", json.dumps(payload))
            except Exception:
                log.exception("Could not persist universe cache to database")
    log.info("Liquidity ranking complete | selected=%d/%d | candidates=%d | failed_or_thin=%d | elapsed=%.1fs",
             len(names), top_n, count, failures, time.monotonic() - started)
    return names
