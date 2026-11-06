"""Check a deployed bot's health/version/diagnostics endpoints.

Usage:
  python tools/validate_deployment.py https://YOUR-SERVICE.onrender.com --key "$CRON_SECRET"

The script validates reachability and configuration indicators; it cannot verify real trade profitability.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def get_json(url, key=None, timeout=12):
    headers = {"Accept": "application/json"}
    if key:
        headers["X-Cron-Key"] = key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            body = {"error": str(exc)}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        # Report network/DNS/TLS/timeout failures instead of aborting with a traceback.
        return 0, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", help="Base URL, e.g. https://nse-fno-bot.onrender.com")
    ap.add_argument("--key", default="", help="CRON_SECRET for protected endpoints")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    failures = 0
    for path in ("/ping", "/version", "/health"):
        code, data = get_json(base + path)
        ok = code == 200 and isinstance(data, dict)
        if path == "/version" and ok and data.get("commit") in (None, "", "unknown"):
            print(f"WARN {path}: reachable, but deployed commit is unknown; check Render commit environment metadata")
        else:
            print(f"{'PASS' if ok else 'FAIL'} {path}: HTTP {code} {json.dumps(data, separators=(',', ':'))[:500]}")
        failures += not ok
    for path in ("/status", "/diagnostics"):
        if not args.key:
            print(f"SKIP {path}: pass --key to check protected endpoint")
            continue
        code, data = get_json(base + path, args.key)
        ok = code == 200 and isinstance(data, dict)
        if path == "/diagnostics" and ok:
            db = data.get("database", {})
            if db.get("persistent_required") and not db.get("postgres_connected"):
                ok = False
                data["validation_warning"] = "persistent DB required but PostgreSQL is not active"
        print(f"{'PASS' if ok else 'FAIL'} {path}: HTTP {code} {json.dumps(data, separators=(',', ':'))[:1200]}")
        failures += not ok
    print("RESULT:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
