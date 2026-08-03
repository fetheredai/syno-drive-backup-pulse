#!/usr/bin/env python3
"""
Mock DSM Web API — enough of it to exercise collector.py end to end without
a real NAS.

This is a test fixture, not a simulator. It answers SYNO.API.Info,
SYNO.API.Auth, SYNO.Core.User, SYNO.Core.Group.Member,
SYNO.SynologyDrive.Connection, SYNO.SynologyDrive.Log, SYNO.FileStation.List
and SYNO.FileStation.DirSize with realistically shaped payloads, including
paging, so the collector's login, log pagination, field picking, day
bucketing, root inference and status thresholds all run against something.

What it does NOT prove: that a real Drive Server uses these exact field
names. That still needs `collector.py --discover` against real hardware —
see the field-mapping section in HANDOFF.md.

  python3 mock_dsm.py 5000        # serve
  python3 -m unittest tests.py    # or let the test suite drive it
"""

import datetime as dt
import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SID = "mock-sid-12345"

# username -> days since last backup activity (None = never backed up)
USER_PROFILE = {
    "alice":  0,     # active today            -> ok
    "brian":  1,     # yesterday               -> ok
    "carol":  5,     # 5 days ago              -> stale
    "dinesh": 21,    # 3 weeks ago             -> failing
    "erin":   None,  # never seen in the log   -> never
}
ADMINS = {"admin", "awhite"}
ROOTS = ["Desktop", "Documents", "Downloads"]

APIS = {
    "SYNO.API.Info":                 {"path": "query.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.API.Auth":                 {"path": "auth.cgi",  "minVersion": 1, "maxVersion": 7},
    "SYNO.Core.User":                {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.Core.Group.Member":        {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.SynologyDrive.Connection": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.SynologyDrive.Log":        {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.FileStation.List":         {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    "SYNO.FileStation.DirSize":      {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
}


def build_log(now=None):
    """Newest-first log covering ~90 days, with per-user activity gaps."""
    now = int(now or time.time())
    rnd = random.Random(1234)          # deterministic, so tests can assert
    events = []
    for user, gap in USER_PROFILE.items():
        if gap is None:
            # Erin logs in but never syncs a file — the exact silent failure
            # this project exists to catch. These must all be filtered out.
            for d in range(0, 90, 7):
                events.append({
                    "username": user,
                    "time": now - d * 86400,
                    "action": "login",
                    "path": "",
                    "device_name": "ERIN-LAPTOP",
                })
            continue
        for d in range(gap, 90):
            if rnd.random() < 0.25:    # not every day has traffic
                continue
            for _ in range(rnd.randint(1, 40)):
                root = rnd.choice(ROOTS)
                events.append({
                    "username": user,
                    "time": now - d * 86400 - rnd.randint(0, 80000),
                    "action": rnd.choice(["upload", "edit", "create_file", "rename"]),
                    "path": f"/homes/{user}/Drive/{user.upper()}-PC/{root}/f{rnd.randint(1,999)}.dat",
                    "device_name": f"{user.upper()}-PC",
                })
    # a couple of admin events that must be excluded from the dashboard
    for d in range(0, 5):
        events.append({"username": "admin", "time": now - d * 86400,
                       "action": "upload", "path": "/homes/admin/Drive/x.txt",
                       "device_name": "ADMIN-PC"})
    events.sort(key=lambda e: e["time"], reverse=True)
    return events


LOG = build_log()


def users_payload(offset, limit):
    names = sorted(USER_PROFILE) + sorted(ADMINS) + ["guest"]
    rows = [{"name": n, "description": n.title() + " User", "expired": "normal"}
            for n in names]
    return {"users": rows[offset:offset + limit], "total": len(rows), "offset": offset}


def connections_payload():
    now = int(time.time())
    out = []
    for user, gap in USER_PROFILE.items():
        # Note: everyone reads as "connected", including users who have not
        # synced in weeks. That is the Admin Console blind spot.
        out.append({
            "username": user,
            "device_name": f"{user.upper()}-PC",
            "last_connection_time": now - (0 if gap is None else gap) * 3600,
            "online": True,
            "client_type": "desktop",
        })
    return {"items": out, "total": len(out)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data):
        self._send({"success": True, "data": data})

    def _err(self, code):
        self._send({"success": False, "error": {"code": code}})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        api, method = q.get("api"), q.get("method")

        if api == "SYNO.API.Info":
            return self._ok(APIS)

        if api == "SYNO.API.Auth":
            if method == "login":
                if q.get("account") != "svc-drivemonitor" or q.get("passwd") != "s3cret":
                    return self._err(400)
                return self._ok({"sid": SID})
            return self._ok({})

        # everything past here needs a session
        if q.get("_sid") != SID:
            return self._err(119)

        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 100))

        if api == "SYNO.Core.User":
            return self._ok(users_payload(offset, limit))

        if api == "SYNO.Core.Group.Member":
            return self._ok({"users": [{"name": n} for n in sorted(ADMINS)],
                             "total": len(ADMINS)})

        if api == "SYNO.SynologyDrive.Connection":
            return self._ok(connections_payload())

        if api == "SYNO.SynologyDrive.Log":
            page = LOG[offset:offset + limit]
            return self._ok({"items": page, "total": len(LOG)})

        if api == "SYNO.FileStation.List":
            folder = q.get("folder_path", "")
            if folder.endswith("/Drive"):
                return self._ok({"files": [{"name": r, "isdir": True} for r in ROOTS]})
            return self._err(408)   # no such file or directory

        if api == "SYNO.FileStation.DirSize":
            if method == "start":
                return self._ok({"taskid": "dirsize-1"})
            return self._ok({"finished": True, "num_file": 1234,
                             "num_dir": 56, "total_size": 987654321})

        return self._err(101)

    def log_message(self, *a):
        pass


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock DSM on http://127.0.0.1:{port}/webapi/  "
          f"(user svc-drivemonitor / s3cret)")
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
