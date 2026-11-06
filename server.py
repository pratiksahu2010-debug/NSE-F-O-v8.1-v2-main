"""HTTP surface for Render free + an external cron service (cron-job.org, UptimeRobot, GitHub Actions ...).

  GET|HEAD /ping                   public, instant, no secrets      -> empty HTTP 204 keep-alive only; never runs bot work
  GET|HEAD /health                 public, instant, no secrets      -> Render's health check
  GET|POST /cron/tick?key=SECRET   run one idempotent bot step in the background, returns immediately
  GET|POST /cron/refresh-tokens    request/validate Upstox token only
  GET      /status?key=SECRET      full status JSON (providers, tokens, last tick/scan)
  GET      /diagnostics?key=SECRET expanded per-symbol rejection, module, database and universe diagnostics
  GET      /report?key=SECRET&days=30[&send=1]   win rate / profit factor by signal type, tier, score, hour, side (+ Telegram)
  POST     /webhook/upstox-token/<secret>   receiver for Upstox's token-approval webhook

`key` may also be sent as header `X-Cron-Key`. With CRON_SECRET unset every protected route answers 403.
Handlers never block on market work: cold-start wake-ups would otherwise exceed the cron service's HTTP timeout.
"""
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from auth.tokens import manager as tokens
from config import cfg

log = logging.getLogger("server")


def _clean_secret(value: str) -> str:
    # Render environment values and HTTP headers can accidentally include whitespace.
    return str(value or "").strip()


def _eq(a: str, b: str) -> bool:
    left, right = _clean_secret(a), _clean_secret(b)
    return bool(left) and bool(right) and hmac.compare_digest(left.encode(), right.encode())


class Runner:
    """Runs a named job in a background thread; refuses to start a second copy while one is running."""

    def __init__(self):
        self._running = {}
        self._lock = threading.Lock()
        self.last = {}

    def start(self, name: str, fn) -> bool:
        with self._lock:
            t = self._running.get(name)
            if t and t.is_alive():
                return False

            def _wrap():
                try:
                    self.last[name] = fn()
                except Exception as e:
                    log.exception("job %s failed", name)
                    self.last[name] = {"error": str(e)[:200]}

            t = threading.Thread(target=_wrap, name=f"job-{name}", daemon=True)
            self._running[name] = t
            t.start()
            return True


def make_handler(bot, runner: Runner):
    class H(BaseHTTPRequestHandler):
        server_version = "fno-bot"

        def log_message(self, *a):
            pass

        # ---- helpers ----
        def _send(self, code: int, obj, head: bool = False):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                if not head:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Health probes and browser clients may disconnect before the body is sent.
                # A closed client is not an application failure and must not trigger a second response.
                return

        def _authed(self, q) -> bool:
            # Supported auth methods: ?key=..., X-Cron-Key, or Authorization: Bearer ...
            key = (q.get("key") or [""])[0]
            if not key:
                key = self.headers.get("X-Cron-Key", "")
            if not key:
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    key = auth[7:].strip()
            configured = _clean_secret(cfg.CRON_SECRET)
            if not configured:
                return False
            return _eq(key, configured)

        def _auth_error(self):
            # Never return or log either secret. Distinguish missing server config from bad client key.
            if not _clean_secret(cfg.CRON_SECRET):
                return {"error": "forbidden", "detail": "CRON_SECRET is not configured in Render environment"}
            return {"error": "forbidden", "detail": "missing or incorrect key; use ?key=..., X-Cron-Key, or Authorization: Bearer ..."}

        def _route(self, method: str):
            u = urlparse(self.path)
            q, path = parse_qs(u.query), u.path.rstrip("/") or "/"
            head = method == "HEAD"
            try:
                if path == "/version":
                    import os
                    commit = os.getenv("RENDER_GIT_COMMIT", os.getenv("GIT_COMMIT", "unknown"))
                    return self._send(200, {"service": "nse-fno-signal-bot",
                                            "version": os.getenv("BOT_VERSION", "nse-fno-v8.1-simple"),
                                            "commit": commit[:12]}, head)
                if path == "/ping":
                    # Keep-alive endpoint is intentionally isolated: no bot, provider, database,
                    # scanner, Telegram, or token-manager calls. HTTP 204 has no response body.
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path in ("/", "/health"):
                    # Public health checks must never wait on provider or database I/O.
                    probe = getattr(bot, "health_probe", None)
                    if callable(probe):
                        return self._send(200, probe(), head)
                    # Compatibility for lightweight test doubles and older integrations.
                    st = bot.health()
                    return self._send(200, {"ok": True, "time": st["time"], "mode": st["mode"],
                                            "booted_for": st["booted_for"],
                                            "provider": st["provider"]["active"]}, head)
                if path == "/status":
                    return self._send(200, bot.health(), head) if self._authed(q) else self._send(403, self._auth_error(), head)
                if path == "/diagnostics":
                    if not self._authed(q):
                        return self._send(403, self._auth_error(), head)
                    fn = getattr(bot, "diagnostics", None)
                    return self._send(200, fn() if callable(fn) else bot.health(), head)
                if path == "/report":
                    if not self._authed(q):
                        return self._send(403, self._auth_error(), head)
                    try:
                        days = int((q.get("days") or ["30"])[0])
                    except ValueError:
                        days = 30
                    out = bot.report(days)
                    if (q.get("send") or ["0"])[0] == "1":
                        bot.tg.send(out["text"])
                    return self._send(200, out, head)
                if path == "/cron/tick":
                    if not self._authed(q):
                        return self._send(403, self._auth_error())
                    if (q.get("wait") or ["0"])[0] == "1":            # debugging: run inline and return the result
                        return self._send(200, bot.tick())
                    started = runner.start("tick", bot.tick)
                    return self._send(202, {"accepted": True, "started": started, "last": bot.last_tick})
                if path == "/cron/refresh-tokens":
                    if not self._authed(q):
                        return self._send(403, self._auth_error())
                    started = runner.start("tokens", lambda: tokens.ensure_fresh(morning=True))
                    return self._send(202, {"accepted": True, "started": started, "tokens": tokens.status()})
                if path.startswith("/webhook/upstox-token/") and method == "POST":
                    secret = path.rsplit("/", 1)[1]
                    if not _eq(secret, cfg.WEBHOOK_SECRET or cfg.CRON_SECRET):
                        return self._send(403, {"error": "forbidden"})
                    n = min(int(self.headers.get("Content-Length") or 0), 8192)
                    try:
                        payload = json.loads(self.rfile.read(n) or b"{}")
                    except ValueError:
                        return self._send(400, {"error": "bad json"})
                    return self._send(200, {"ok": tokens.handle_upstox_webhook(payload)})
                return self._send(404, {"error": "not found"}, head)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:           # health checks must never crash the process
                log.exception("request failed")
                return self._send(500, {"ok": False, "error": str(e)[:200]}, head)

        def do_GET(self):
            self._route("GET")

        def do_HEAD(self):
            self._route("HEAD")

        def do_POST(self):
            self._route("POST")

    return H


def start_server(port: int, bot):
    runner = Runner()
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(bot, runner))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    return srv
