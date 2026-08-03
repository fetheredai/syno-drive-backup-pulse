#!/usr/bin/env python3
"""
Mock DSM Web API — shaped from a real Synology Drive Server 3.1 response,
captured from a live NAS via probe2.py.

This is not a guess. The Log and Connection payloads reproduce the actual
field names, including the awkward ones:

  * the file path is in `s1`, the client device in `s2`
  * `type` is a NUMERIC action code (13 = file event), not a verb
  * there is a key literally called `target` whose value is "user" — which
    will be mistaken for the file path by any naive field-name search
  * on Connection, `client_name` is the USERNAME and `client_id` is the DEVICE
  * SYNO.SynologyDrive.Log rejects calls without `target` and `share_type`

    python3 mock_dsm.py 5000
    python3 -m unittest tests -v
"""

import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SID = "mock-sid-12345"

# username -> days since last backup activity (None = never moved a file)
USER_PROFILE = {
    "alice":  0,     # active today            -> ok
    "brian":  1,     # yesterday               -> ok
    "carol":  5,     # 5 days ago              -> stale
    "dinesh": 21,    # 3 weeks ago             -> failing
    "erin":   None,  # authenticates, never syncs -> never
}
ADMINS = {"admin", "awhite"}
# DSM accounts with no Drive client at all — no connection, no log entries.
# On a real NAS these are the majority, and they must be distinguishable from
# a user whose backup stopped.
EXTRA_USERS = ["frank", "gina"]
ROOTS = ["Desktop", "Documents", "Downloads"]

# Accounts that can authenticate, for exercising dashboard login.
ACCOUNTS = {"svc-drivemonitor": "s3cret", "alice": "alicepw", "carol": "carolpw",
            "twofa": "twofapw"}
TWO_FACTOR_ACCOUNTS = {"twofa"}      # DSM returns 403 for these
VIEWER_GROUP = "DriveViewers"
VIEWER_MEMBERS = {"alice"}

TYPE_FILE = 13      # observed on real hardware for file events
TYPE_AUTH = 1       # non-file event; carries no s1 path

APIS = {
    "SYNO.API.Info":                 {"path": "query.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.API.Auth":                 {"path": "auth.cgi",  "minVersion": 1, "maxVersion": 7},
    "SYNO.Core.User":                {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.Core.Group.Member":        {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.SynologyDrive.Connection": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    "SYNO.SynologyDrive.Log":        {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    "SYNO.FileStation.List":         {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    "SYNO.FileStation.DirSize":      {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
}


def _device(user):
    return f"BNO-{user.capitalize()}-451-L379LTDXR2.local"


def log_item(user, ts, root=None, seq=0):
    """One log row in the real shape."""
    is_file = root is not None
    return {
        "accessable": False,
        "client_type": "drive_backup",
        "filestation_link_prefix": f"/homes/{user}/Drive",
        "ip_address": "72.76.98.186",
        "p1": "",
        "p2": "0",
        # The real backup layout: /Backup/<device>/Users/<localuser>/<Root>/...
        "s1": (f"/Backup/{_device(user)}/Users/{user}win/{root}/f{seq}.docx"
               if is_file else ""),
        "s2": _device(user),
        "s3": "",
        "share_name": user,
        "share_type": 0,
        # Deliberately present, deliberately not a path. Any field-name search
        # that looks for "target" will pick this up and be wrong.
        "target": "user",
        "target_accessable": False,
        "target_link_prefix": "/homes//Drive",
        "target_share_name": "",
        "target_share_type": 0,
        "time": ts,
        "type": TYPE_FILE if is_file else TYPE_AUTH,
        "username": user,
    }


def build_log(now=None):
    now = int(now or time.time())
    rnd = random.Random(1234)
    events = []
    for user, gap in USER_PROFILE.items():
        if gap is None:
            # Erin's client authenticates regularly but never moves a file.
            # These carry no s1, and must not count as backups.
            for d in range(0, 90, 7):
                events.append(log_item(user, now - d * 86400))
            continue
        for d in range(gap, 90):
            if rnd.random() < 0.25:
                continue
            for i in range(rnd.randint(1, 40)):
                events.append(log_item(user, now - d * 86400 - rnd.randint(0, 80000),
                                       root=rnd.choice(ROOTS), seq=i))
        if user == "carol":
            # A path that does not follow /Backup/<dev>/Users/<u>/<Root>/.
            # It must yield no root at all rather than "Backup" or the
            # device hostname, which is what the old fallback produced.
            odd = log_item(user, now - gap * 86400, root="X")
            odd["s1"] = f"/Backup/{_device(user)}/loose-file.txt"
            events.append(odd)
    for d in range(0, 5):
        events.append(log_item("admin", now - d * 86400, root="Desktop"))
    events.sort(key=lambda e: e["time"], reverse=True)
    return events


LOG = build_log()


def users_payload(offset, limit):
    names = sorted(USER_PROFILE) + EXTRA_USERS + sorted(ADMINS) + ["guest"]
    rows = [{"name": n, "description": n.title() + " User", "expired": "normal"}
            for n in names]
    return {"users": rows[offset:offset + limit], "total": len(rows), "offset": offset}


def connections_payload():
    now = int(time.time())
    out = []
    for user, gap in USER_PROFILE.items():
        # Everyone reads as on_line, including users who have not synced in
        # weeks. That is precisely the Admin Console blind spot.
        out.append({
            "client_can_wipe": False,
            "client_id": _device(user),          # DEVICE
            "client_ip": "192.168.50.232",
            "client_is_relay": False,
            "client_location": "",
            "client_name": user,                 # USERNAME
            "client_session_id": "b80294a36fb9b6dbe942fc2e827b7105",
            "client_status": "on_line",
            "client_type": "drive_backup",
            "client_version": "3.5.0-16084",
            "device_uuid": "3a6a4c02-fc0d-43de-9374-5f9e3b5845a3",
            "last_auth_time": now - (0 if gap is None else gap) * 3600,
            "login_time": str(now - (0 if gap is None else gap) * 3600),
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

    def _err(self, code, name=None, reason=None):
        err = {"code": code}
        if name:
            err["errors"] = {"name": name, "reason": reason or "required"}
        self._send({"success": False, "error": err})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        api, method = q.get("api"), q.get("method")

        if api == "SYNO.API.Info":
            return self._ok(APIS)

        if api == "SYNO.API.Auth":
            if method == "login":
                if ACCOUNTS.get(q.get("account")) != q.get("passwd"):
                    return self._err(400)
                if q.get("account") in TWO_FACTOR_ACCOUNTS:
                    return self._err(403)      # OTP required
                return self._ok({"sid": SID})
            return self._ok({})

        if q.get("_sid") != SID:
            return self._err(119)

        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 100))

        if api == "SYNO.Core.User":
            return self._ok(users_payload(offset, limit))

        if api == "SYNO.Core.Group.Member":
            group = q.get("group")
            if group == VIEWER_GROUP:
                return self._ok({"users": [{"name": n} for n in sorted(VIEWER_MEMBERS)],
                                 "total": len(VIEWER_MEMBERS)})
            if group == "administrators":
                return self._ok({"users": [{"name": n} for n in sorted(ADMINS)],
                                 "total": len(ADMINS)})
            return self._err(105)          # no such group

        if api == "SYNO.SynologyDrive.Connection":
            return self._ok(connections_payload())

        if api == "SYNO.SynologyDrive.Log":
            # The real server refuses without these, naming each in turn.
            if "target" not in q:
                return self._err(120, "target")
            if "share_type" not in q:
                return self._err(120, "share_type")
            page = LOG[offset:offset + limit]
            return self._ok({"items": page, "total": len(LOG)})

        if api == "SYNO.FileStation.List":
            # Real behaviour observed: other users' homes are not listable by
            # the service account, so root inference cannot rely on this.
            return self._err(408)

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
