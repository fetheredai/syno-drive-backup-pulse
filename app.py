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

import json
import os
import signal
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import collector

WEB_DIR = os.environ.get("SYNO_WEB_DIR", "/app/web")
PORT = int(os.environ.get("SYNO_WEB_PORT", "8477"))
INTERVAL_H = float(os.environ.get("SYNO_INTERVAL_HOURS", "4"))
RUN_AT_START = os.environ.get("SYNO_RUN_AT_START", "true").strip().lower() \
    in ("1", "true", "yes", "on")

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
    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            body = json.dumps({
                "status": "ok" if _state["last_ok"] else
                          ("starting" if _state["last_run"] is None else "degraded"),
                **_state,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", ""):
            self.path = "/dashboard.html"
        return super().do_GET()

    def end_headers(self):
        # data.json changes every collection; never let a browser cache it.
        if self.path.split("?")[0].endswith("data.json"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *a):
        pass  # keep container logs to collector output only


def shutdown(signum, _frame):
    log(f"signal {signum}, shutting down")
    _stop.set()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

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
