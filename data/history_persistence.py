"""Non-blocking, isolated persistence for historical candle warmup.

Candle writes run on a dedicated daemon worker and a separate DB connection
so a slow or stalled history write cannot hold the scanner's primary DB lock
or block scanner construction.
"""
import logging
import queue
import threading
import time


log = logging.getLogger("history_persistence")


class AsyncCandleWriter:
    """Queue durable candle checkpoints without blocking the market-data path."""

    def __init__(self, db_factory, max_queue=256):
        self._db_factory = db_factory
        self._queue = queue.Queue(maxsize=max(1, int(max_queue)))
        self._lock = threading.RLock()
        self._thread = None
        self._db = None
        self._active = None
        self._queued = 0
        self._written = 0
        self._failed = 0
        self._last_error = None
        self._closed = False

    def _ensure_worker(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("historical candle writer is closed")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="history-candle-writer", daemon=True
                )
                self._thread.start()

    def submit(self, symbol, candles):
        """Queue a symbol checkpoint; returns immediately or raises if saturated."""
        if not candles:
            return False
        self._ensure_worker()
        payload = (str(symbol), list(candles))
        try:
            self._queue.put_nowait(payload)
        except queue.Full as exc:
            raise RuntimeError("historical candle persistence queue is full") from exc
        with self._lock:
            self._queued += 1
        return True

    def _run(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    if self._db is not None:
                        close = getattr(self._db, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                log.exception("history candle writer database close failed")
                    return
                symbol, candles = item
                started = time.monotonic()
                with self._lock:
                    self._active = {"symbol": symbol, "started": started}
                try:
                    if self._db is None:
                        self._db = self._db_factory()
                    self._db.save_candles(symbol, candles)
                    with self._lock:
                        self._written += 1
                        self._last_error = None
                    log.info(
                        "history candle persistence complete | symbol=%s | bars=%d | elapsed=%.1fs",
                        symbol, len(candles), time.monotonic() - started,
                    )
                except Exception as exc:
                    with self._lock:
                        self._failed += 1
                        self._last_error = {
                            "symbol": symbol,
                            "error": f"{type(exc).__name__}: {exc}"[:240],
                        }
                    log.exception("history candle persistence failed | symbol=%s", symbol)
                finally:
                    with self._lock:
                        self._active = None
            finally:
                self._queue.task_done()

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            active = None
            if self._active:
                active = {
                    "symbol": self._active["symbol"],
                    "elapsed_seconds": round(now - self._active["started"], 1),
                }
            return {
                "queued": self._queued,
                "pending": self._queue.qsize(),
                "written": self._written,
                "failed": self._failed,
                "active": active,
                "last_error": self._last_error,
                "worker_alive": bool(self._thread and self._thread.is_alive()),
            }

    def close(self, timeout=2.0):
        """Ask the daemon worker to close; never wait indefinitely on remote I/O."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            log.warning(
                "history candle writer queue full at shutdown | pending=%d",
                self._queue.qsize(),
            )
            return
        thread.join(timeout=max(0.0, float(timeout)))
        if thread.is_alive():
            log.warning(
                "history candle writer still active at shutdown | timeout=%.1fs | state=%s",
                timeout, self.snapshot(),
            )
