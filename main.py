import logging
import os
import signal
import threading
import time

from config import cfg
from server import start_server
from utils import logger

logger.setup()
log = logging.getLogger("main")


_STARTUP_ALERT = {"at": None}


def _alert_startup_failure(exc, cooldown=1800.0):
    """Bot() failed before it had a Telegram client of its own (bad DB/env). Without this, only the logs show it."""
    now = time.monotonic()
    if _STARTUP_ALERT["at"] is not None and now - _STARTUP_ALERT["at"] < cooldown:
        return
    _STARTUP_ALERT["at"] = now
    try:
        import html
        from telegram_service import Telegram
        Telegram().send(f"🛑 <b>F&amp;O bot failed to start</b>\n{html.escape(type(exc).__name__)}: "
                        f"{html.escape(str(exc)[:300])}\nRetrying every 15s. Check the Render environment "
                        "(DATABASE_URL, Upstox token) and logs.")
    except Exception:
        log.exception("startup failure alert could not be sent")


class _BotProxy:
    """Keep HTTP health endpoints available while the heavier bot initializes."""

    def __init__(self):
        self._target = None
        self._lock = threading.Lock()
        self._phase = "http_starting"
        self._last_error = None
        self._phase_changed_at = time.time()

    def set_phase(self, phase, error=None):
        with self._lock:
            self._phase = str(phase)
            self._last_error = str(error)[:240] if error else None
            self._phase_changed_at = time.time()

    def attach(self, bot):
        with self._lock:
            self._target = bot
            self._phase = "ready"
            self._last_error = None
            self._phase_changed_at = time.time()

    def current(self):
        with self._lock:
            return self._target

    def _startup_snapshot(self):
        with self._lock:
            return {
                "phase": self._phase,
                "last_error": self._last_error,
                "phase_age_seconds": round(max(0.0, time.time() - self._phase_changed_at), 1),
            }

    def health_probe(self):
        """Fast in-memory health snapshot; never touches providers, databases, or token services."""
        bot = self.current()
        startup = self._startup_snapshot()
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        if bot is None:
            return {"ok": True, "time": now, "mode": "starting",
                    "booted_for": None, "startup": startup,
                    "scanner": {"available": False, "stage": "not_initialized", "running": False}}
        probe = getattr(bot, "health_probe", None)
        if callable(probe):
            result = probe()
            result["startup"] = startup
            return result
        scanner = getattr(bot, "scanner", None)
        stage = getattr(scanner, "last_cycle_stage", "not_bootstrapped") if scanner else "not_bootstrapped"
        running = bool(scanner and stage not in {"idle", "completed", "failed"})
        return {
            "ok": True, "time": now,
            "mode": "loop+cron" if cfg.RUN_LOOP else "cron-only",
            "booted_for": getattr(bot, "boot_day", None),
            "startup": startup,
            "scanner": {
                "available": scanner is not None,
                "stage": stage,
                "running": running,
                "last_cycle_started_at": getattr(scanner, "last_cycle_started_at", None) if scanner else None,
                "last_cycle_at": getattr(scanner, "last_cycle_at", None) if scanner else None,
                "last_cycle_error": getattr(scanner, "last_cycle_error", None) if scanner else None,
            },
        }

    def health(self):
        bot = self.current()
        if bot is None:
            state = self._startup_snapshot()
            return {
                "ok": True,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "provider": {"active": "starting"},
                "mode": "starting",
                "booted_for": None,
                "startup": state,
            }
        result = bot.health()
        result["startup"] = self._startup_snapshot()
        return result

    def diagnostics(self):
        bot = self.current()
        if bot is None:
            return {"status": "starting", "startup": self._startup_snapshot()}
        result = bot.diagnostics()
        result["startup"] = self._startup_snapshot()
        return result

    def __getattr__(self, name):
        bot = self.current()
        if bot is None:
            raise RuntimeError("bot is still initializing")
        return getattr(bot, name)


def _loop(bot, stop):
    while not stop.is_set() and not getattr(bot, "_stop_event", threading.Event()).is_set():
        try:
            bot.run()
        except Exception:
            log.exception("bot loop crashed; restarting in 15s")
            stop.wait(15)


def main():
    # Install signal handlers before starting background work so shutdowns
    # during cold start are recorded consistently.
    stop = threading.Event()

    def _handle_shutdown_signal(signum, _frame):
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, TypeError):
            signal_name = str(signum)
        log.warning("shutdown signal received | signal=%s | pid=%s", signal_name, os.getpid())
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_shutdown_signal)

    # Bind Render's required port before database, token, instrument-master,
    # historical-data, or websocket initialization can delay startup.
    proxy = _BotProxy()
    srv = start_server(cfg.PORT, proxy)
    proxy.set_phase("http_bound")
    log.info("HTTP bound on 0.0.0.0:%s | bot initialization pending", cfg.PORT)
    if not cfg.CRON_SECRET:
        log.warning("CRON_SECRET is not set: /cron/* and /status are disabled (403). Set it to use an external cron.")

    def _initialize_bot():
        while not stop.is_set():
            try:
                proxy.set_phase("importing_bot")
                log.info("Bot initialization thread started | importing bot module")
                from bot import Bot
                if stop.is_set():
                    return
                proxy.set_phase("constructing_bot")
                log.info("Bot module imported | constructing Bot (DB, providers, Telegram, scanner dependencies)")
                bot = Bot()
                if stop.is_set():
                    try:
                        bot.shutdown()
                    except Exception:
                        log.exception("bot shutdown after cancelled initialization failed")
                    return
                proxy.attach(bot)
                log.info("Bot initialization complete | mode=%s | db=%s",
                         "loop+cron" if cfg.RUN_LOOP else "cron-only", bot.db.backend)
                if cfg.RUN_LOOP:
                    log.info("Starting bot loop thread")
                    threading.Thread(target=_loop, args=(bot, stop),
                                     name="bot-loop", daemon=True).start()
                else:
                    log.info("RUN_LOOP disabled | cron-only mode")
                return
            except Exception as exc:
                proxy.set_phase("retry_wait", exc)
                log.exception("bot initialization failed; retrying in 15s while HTTP health remains available")
                _alert_startup_failure(exc)
                stop.wait(15)

    initializer = threading.Thread(target=_initialize_bot, name="bot-bootstrap", daemon=True)
    initializer.start()

    try:
        stop.wait()
    finally:
        proxy.set_phase("shutting_down")
        log.info("shutting down | pid=%s | initializer_alive=%s", os.getpid(), initializer.is_alive())
        # Bot() may still be constructing dependencies when Render sends SIGTERM.
        # Give it a bounded chance to observe stop and run cancellation cleanup.
        initializer.join(timeout=20.0)
        if initializer.is_alive():
            log.error("bot initializer did not stop within grace period | timeout=20s")
        else:
            log.info("bot initializer stopped")
        bot = proxy.current()
        if bot is not None:
            try:
                bot.shutdown()
            except Exception:
                log.exception("bot shutdown failed")
            try:
                bot.tg.flush(5)
            except Exception:
                log.exception("telegram flush failed")
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            log.exception("HTTP server shutdown failed")


if __name__ == "__main__":
    main()
