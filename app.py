#!/usr/bin/env python3
"""
Container entrypoint: serves the dashboard and runs the collector on a loop.

One process, two jobs:
  - a background thread that runs a collection, sleeps SYNO_INTERVAL_HOURS,
    and repeats
  - a threaded HTTP server on SYNO_WEB_PORT serving web/, with data.json
    marked no-store so a refresh always shows the latest collection

Endpoints:
  /             the dashboard
  /data.json    collector output
  /healthz      JSON: last run time, last run result, next run time

Run a collection immediately without waiting for the loop:
  docker exec <container> python3 collector.py
"""

import http.cookies
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import auth
import collector

WEB_DIR = os.environ.get("SYNO_WEB_DIR", "/app/web")
PORT = int(os.environ.get("SYNO_WEB_PORT", "8477"))
INTERVAL_H = float(os.environ.get("SYNO_INTERVAL_HOURS", "4"))
RUN_AT_START = os.environ.get("SYNO_RUN_AT_START", "true").strip().lower() \
    in ("1", "true", "yes", "on")
# Login is on by default: the dashboard lists usernames, device names and
# backup health. SYNO_AUTH=false disables it for a trusted network.
AUTH_ON = os.environ.get("SYNO_AUTH", "true").strip().lower() \
    in ("1", "true", "yes", "on")
MAX_LOGIN_BODY = 8192

_state = {
    "last_run": None,
    "last_ok": None,
    "last_error": None,
    "next_run": None,
    "runs": 0,
}
_stop = threading.Event()


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def collect_loop():
    cfg_path = os.environ.get("SYNO_CONFIG", "/app/config.json")
    if not RUN_AT_START:
        _state["next_run"] = int(time.time() + INTERVAL_H * 3600)
        log(f"SYNO_RUN_AT_START is false; first collection in {INTERVAL_H}h")
        if _stop.wait(INTERVAL_H * 3600):
            return
    while not _stop.is_set():
        try:
            cfg = collector.load_config(cfg_path)
            days = cfg.get("days", 90)
            output = cfg.get("output") or os.path.join(WEB_DIR, "data.json")
            counts = cfg.get("file_counts", False)
            log(f"collection starting (window {days}d, file_counts={counts})")
            ok = collector.run_once(cfg, days, output, counts)
            _state["last_ok"] = ok
            _state["last_error"] = None if ok else "one or more NASes failed"
            log("collection finished" + ("" if ok else " WITH ERRORS"))
        except SystemExit as e:
            # collector.load_config calls sys.exit on missing credentials —
            # surface it instead of silently killing the thread.
            _state["last_ok"] = False
            _state["last_error"] = str(e)
            log(f"collection aborted: {e}")
        except Exception as e:
            _state["last_ok"] = False
            _state["last_error"] = repr(e)
            log(f"collection failed: {e!r}")
        _state["last_run"] = int(time.time())
        _state["runs"] += 1
        _state["next_run"] = int(time.time() + INTERVAL_H * 3600)
        if _stop.wait(INTERVAL_H * 3600):
            return


class Handler(SimpleHTTPRequestHandler):
    # -- helpers ------------------------------------------------------------
    def _send_bytes(self, body, ctype, code=200, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, to, headers=()):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def _token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return http.cookies.SimpleCookie(raw).get(
                auth.SESSION_COOKIE).value  # type: ignore[union-attr]
        except Exception:
            return None

    def _current_user(self):
        return auth.session_user(self._token())

    def _is_loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/healthz":
            # The container healthcheck calls this over loopback, so it must
            # work without a session; from anywhere else it needs one, since
            # it reveals collection state.
            if AUTH_ON and not self._is_loopback() and not self._current_user():
                return self._send_bytes(b'{"error":"auth required"}',
                                        "application/json", 401)
            body = json.dumps({
                "status": "ok" if _state["last_ok"] else
                          ("starting" if _state["last_run"] is None else "degraded"),
                **_state,
            }).encode()
            return self._send_bytes(body, "application/json")

        if path == "/login":
            if not AUTH_ON:
                return self._redirect("/")
            return self._send_bytes(auth.login_page(), "text/html; charset=utf-8")

        if path == "/logout":
            token = self._token()
            if token:
                auth.end_session(token)
            return self._redirect("/login", [(
                "Set-Cookie",
                f"{auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")])

        if AUTH_ON and not self._current_user():
            return self._redirect("/login")

        if self.path in ("/", ""):
            self.path = "/dashboard.html"
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/login" or not AUTH_ON:
            return self._send_bytes(b"Not found", "text/plain", 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_LOGIN_BODY:
            return self._send_bytes(auth.login_page("Malformed request."),
                                    "text/html; charset=utf-8", 400)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(
            "utf-8", "replace"))
        user = (form.get("username") or [""])[0]
        pw = (form.get("password") or [""])[0]

        ok, msg = auth.check_credentials(user, pw)
        if not ok:
            log(f"login failed for {user!r} from {self.client_address[0]}: {msg}")
            return self._send_bytes(auth.login_page(msg),
                                    "text/html; charset=utf-8", 401)

        token = auth.new_session(user)
        log(f"login ok for {user!r} from {self.client_address[0]}")
        # No Secure flag: this server speaks plain HTTP. Put it behind a
        # reverse proxy with TLS if it leaves a trusted network.
        return self._redirect("/", [(
            "Set-Cookie",
            f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")])

    def end_headers(self):
        # Everything here is either live status or behind a login, so nothing
        # should sit in a browser cache. Without this, a logged-out user could
        # still see the dashboard from cache — the browser never revalidates,
        # so the redirect to /login never happens.
        if not self._cache_header_sent:
            self._cache_header_sent = True
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def send_response(self, *a, **kw):
        self._cache_header_sent = False
        return super().send_response(*a, **kw)

    _cache_header_sent = False

    def log_message(self, fmt, *a):
        pass  # keep container logs to collector output only


def shutdown(signum, _frame):
    log(f"signal {signum}, shutting down")
    _stop.set()
    sys.exit(0)


def preflight():
    """Check at startup what would otherwise only fail on someone's first
    login attempt, hours later, with a misleading message."""
    if not AUTH_ON:
        log("login is DISABLED (SYNO_AUTH=false) — anyone who can reach the "
            "port can read the dashboard")
        return
    try:
        auth.service_password()
    except Exception as e:
        log(f"CONFIG PROBLEM: {e}")
        log("  Sign-in will fail until this is fixed: verifying group "
            "membership needs the service account.")
        return
    if auth.LOGIN_GROUP:
        try:
            members = auth.group_members(auth.LOGIN_GROUP)
            log(f"sign-in restricted to '{auth.LOGIN_GROUP}' "
                f"({len(members)} members)")
        except Exception as e:
            log(f"CONFIG PROBLEM: cannot read group "
                f"'{auth.LOGIN_GROUP}': {e}")
            log("  Sign-in will be refused for everyone until this is fixed, "
                "because membership cannot be confirmed.")
    else:
        log("sign-in open to any DSM account (SYNO_LOGIN_GROUP unset)")


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    preflight()
    threading.Thread(target=collect_loop, daemon=True).start()

    handler = partial(Handler, directory=WEB_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    log(f"dashboard on http://0.0.0.0:{PORT}/  (collect every {INTERVAL_H}h)")
    try:
        server.serve_forever()
    finally:
        _stop.set()


if __name__ == "__main__":
    main()
