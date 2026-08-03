#!/usr/bin/env python3
"""
Synology Drive Backup Status Collector
--------------------------------------
Pulls per-user Synology Drive sync/backup activity from the NAS using the DSM
Web API (the same endpoints the Drive Admin Console web UI calls), aggregates
it into a JSON file, and that JSON feeds web/dashboard.html.

Normal deployment is one container per NAS, running ON the NAS it monitors, so
the default target is the local DSM over plain HTTP on localhost:5000 — that
traffic never leaves the box, so there are no certificate or firewall problems
to solve.

Data collected per non-admin user:
  - Devices (client list): device name, last connection time, status
  - Daily sync activity for the last N days (from the Drive Server log)
  - Root folders being backed up (Desktop, Documents, ...) inferred from
    logged file paths, with optional file counts via File Station DirSize
  - Overall status: ok / stale / failing / never

CONFIGURATION
  Two ways, environment variables being the container-friendly one:

  1. Environment variables (used when no config.json is present):
       SYNO_NAS_NAME    label shown in the dashboard      (default: hostname)
       SYNO_HOST        DSM host                          (default: localhost)
       SYNO_PORT        DSM port                          (default: 5000)
       SYNO_HTTPS       true/false                        (default: false)
       SYNO_VERIFY_SSL  true/false                        (default: false)
       SYNO_BASE_URL    full URL, overrides host/port/https  (optional)
       SYNO_USER        service account name              (required)
       SYNO_PASS        service account password
       SYNO_PASS_FILE   file to read the password from (Docker secrets)
       SYNO_DAYS        history window in days            (default: 90)
       SYNO_OUTPUT      output path            (default: web/data.json)
       SYNO_FILE_COUNTS true/false, per-root file counts  (default: false)

  2. config.json (see config.example.json) — still supports several NASes in
     one run, for a central collector rather than one container per NAS.

IMPORTANT NOTE ON ENDPOINTS
  The Drive Admin Console APIs (SYNO.SynologyDrive.*) are not publicly
  documented by Synology and field names can vary between Drive Server
  versions. This script:
    1. Auto-discovers available API names/versions via SYNO.API.Info.
    2. Uses defensive parsing (multiple candidate field names).
    3. Has a --discover mode that prints every SYNO.SynologyDrive.* API
       your NAS exposes, plus a sample log entry, so you can adjust the
       CANDIDATE_* constants below if your Drive Server version differs.
  If something comes back empty, run:  python3 collector.py --discover
  and compare with what you see in browser DevTools (Network tab) while
  using Drive Admin Console.

Requires: Python 3.8+, requests  (pip install requests)
"""

import argparse
import datetime as dt
import json
import os
import socket
import sys
import time
from collections import defaultdict

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Candidate API names / parameters. Verified against Drive Server 3.x era
# behavior; adjust after running --discover if your version differs.
# ---------------------------------------------------------------------------
CANDIDATE_CONNECTION_APIS = [
    "SYNO.SynologyDrive.Connection",   # client list (devices)
]
CANDIDATE_LOG_APIS = [
    "SYNO.SynologyDrive.Log",          # Drive Admin Console > Log
]

# SYNO.SynologyDrive.Log refuses to return anything without these. It names
# the missing one in error 120, which is how they were found (see probe2.py).
# "all"/"all" is the widest scope; narrow it only if you want a subset.
LOG_PARAMS = {"target": "all", "share_type": "all"}

# Log item fields we look for (first match wins). Verified against Drive
# Server 3.1 on real hardware.
#   - the file path is s1, NOT "path"
#   - the client device is s2
#   - "target" is present but holds "user", not a path. It must never appear
#     in F_PATH or every event's root becomes "user".
#   - on Connection, client_name is the USER and client_id is the DEVICE
F_USER = ["username", "client_name", "user", "owner", "opuser", "user_name"]
F_TIME = ["time", "timestamp", "utime", "mtime", "log_time"]
F_PATH = ["s1", "path", "file_path", "display_path", "filename"]
F_ACTION = ["action", "category", "type", "event", "method"]
F_DEVICE = ["s2", "client_id", "device_name", "device", "computer_name", "hostname"]

# Log actions that count as "data moved" (a backup actually did something).
SYNC_ACTIONS = {"upload", "edit", "create", "add", "modify", "sync",
                "create_file", "create_folder", "rename", "download"}
# Actions we ignore entirely
IGNORE_ACTIONS = {"login", "logout", "browse", "preview"}


class SynoSession:
    """Thin DSM Web API client with API auto-discovery."""

    def __init__(self, name, host="localhost", port=5000, https=False,
                 verify_ssl=False, username=None, password=None, base_url=None):
        self.name = name
        if base_url:
            self.base = base_url.rstrip("/") + "/webapi"
        else:
            scheme = "https" if https else "http"
            self.base = f"{scheme}://{host}:{port}/webapi"
        self.verify = verify_ssl
        self.username = username
        self.password = password
        self.sid = None
        self.apis = {}          # api name -> {path, minVersion, maxVersion}
        self.s = requests.Session()

    # -- plumbing -----------------------------------------------------------
    def _get(self, path, params):
        r = self.s.get(f"{self.base}/{path}", params=params,
                       verify=self.verify, timeout=30)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(
                f"[{self.name}] Non-JSON reply from {self.base}/{path} "
                f"(HTTP {r.status_code}). Wrong port, or DSM redirected to a "
                f"login page — check SYNO_HOST/SYNO_PORT/SYNO_HTTPS.")
        if not data.get("success"):
            err = data.get("error") or {}
            code = err.get("code")
            hint = AUTH_ERRORS.get(code, "")
            raise RuntimeError(f"[{self.name}] API error on {params.get('api')}: "
                               f"{json.dumps(err)}{hint}")
        return data.get("data", {})

    def call(self, api, method, version=None, **kw):
        info = self.apis.get(api)
        if not info:
            raise RuntimeError(f"[{self.name}] API not available on this NAS: {api}")
        params = {
            "api": api,
            "method": method,
            "version": version or info["maxVersion"],
            "_sid": self.sid,
        }
        params.update(kw)
        return self._get(info["path"], params)

    # -- auth ---------------------------------------------------------------
    def connect(self):
        # Discover all APIs (paths + versions) first — this is the documented
        # bootstrap step and also powers --discover.
        self.apis = self._get("query.cgi", {
            "api": "SYNO.API.Info", "method": "query",
            "version": 1, "query": "all",
        })
        auth = self.apis.get("SYNO.API.Auth", {"path": "auth.cgi", "maxVersion": 6})
        ver = min(auth.get("maxVersion", 6), 6)
        data = self._get(auth["path"], {
            "api": "SYNO.API.Auth", "method": "login", "version": ver,
            "account": self.username, "passwd": self.password,
            "session": "DriveMonitor", "format": "sid",
        })
        self.sid = data["sid"]

    def logout(self):
        try:
            auth = self.apis.get("SYNO.API.Auth")
            if auth and self.sid:
                self._get(auth["path"], {"api": "SYNO.API.Auth", "method": "logout",
                                         "version": 1, "session": "DriveMonitor",
                                         "_sid": self.sid})
        except Exception:
            pass

    # -- users --------------------------------------------------------------
    def list_users(self, page_size=200, max_pages=50):
        """SYNO.Core.User.list is paged; walk it so big directories aren't cut off."""
        users, offset = [], 0
        for _ in range(max_pages):
            data = self.call("SYNO.Core.User", "list",
                             offset=offset, limit=page_size,
                             additional=json.dumps(["email", "expired", "description"]))
            page = data.get("users", [])
            users.extend(page)
            total = data.get("total")
            offset += len(page)
            if not page or (total is not None and offset >= total) or len(page) < page_size:
                break
        return users

    def group_members(self, group, page_size=500, max_pages=20):
        """Usernames in a DSM group. Raises if the group cannot be read."""
        members, offset = set(), 0
        for _ in range(max_pages):
            data = self.call("SYNO.Core.Group.Member", "list", group=group,
                             offset=offset, limit=page_size)
            page = data.get("users", [])
            members |= {u.get("name") for u in page if u.get("name")}
            total = data.get("total")
            offset += len(page)
            if not page or (total is not None and offset >= total) \
                    or len(page) < page_size:
                break
        return members

    def admin_usernames(self):
        try:
            return self.group_members("administrators")
        except Exception as e:
            print(f"  ! Could not list administrators group ({e}); "
                  f"only excluding built-in 'admin'.")
            return {"admin"}

    # -- drive: client list -------------------------------------------------
    def drive_connections(self):
        last_err = None
        for api in CANDIDATE_CONNECTION_APIS:
            if api not in self.apis:
                continue
            try:
                data = self.call(api, "list")
                for key in ("items", "connections", "list", "clients"):
                    if key in data:
                        return data[key]
                return data if isinstance(data, list) else []
            except Exception as e:
                last_err = e
        if last_err:
            print(f"  ! Client list unavailable: {last_err}")
        return []

    # -- drive: log ---------------------------------------------------------
    def drive_log(self, since_epoch, page_size=1000, max_pages=None):
        """Page through the Drive Server log, newest first, until since_epoch."""
        if max_pages is None:
            max_pages = int(os.environ.get("SYNO_MAX_LOG_PAGES", "500"))
        api = next((a for a in CANDIDATE_LOG_APIS if a in self.apis), None)
        if not api:
            print("  ! No SYNO.SynologyDrive.Log API found. Run --discover.")
            return []
        items, offset = [], 0
        reached_window = False
        for _ in range(max_pages):
            try:
                data = self.call(api, "list", offset=offset, limit=page_size,
                                 **LOG_PARAMS)
            except Exception as e:
                print(f"  ! Drive log page failed at offset {offset}: {e}")
                break
            page = None
            for key in ("items", "logs", "list", "data"):
                if isinstance(data.get(key), list):
                    page = data[key]
                    break
            if not page:
                break
            items.extend(page)
            # Only stop early if this page actually carried usable timestamps;
            # a page of unparseable times must not look like "we reached 1970".
            stamps = [t for t in (pick_int(i, F_TIME) for i in page) if t]
            offset += len(page)
            if stamps and min(stamps) < since_epoch:
                reached_window = True
                break
            if len(page) < page_size:
                reached_window = True
                break
        if not reached_window:
            # We stopped on the page cap rather than on reaching the start of
            # the window. Everything older than this point is missing, so a
            # user whose only activity predates it would wrongly read "never".
            # Say so loudly rather than publish a quietly-truncated calendar.
            print(f"  ! Stopped after {max_pages} pages ({len(items)} events) "
                  f"without reaching the start of the window. History is "
                  f"TRUNCATED and statuses may be wrong. Raise "
                  f"SYNO_MAX_LOG_PAGES or lower SYNO_DAYS.")
        return [i for i in items if (pick_int(i, F_TIME) or 0) >= since_epoch]

    # -- file counts --------------------------------------------------------
    def dir_stats(self, path, timeout_s=120):
        """File/folder counts + size via File Station DirSize (async task)."""
        start = self.call("SYNO.FileStation.DirSize", "start", path=json.dumps([path]))
        taskid = start.get("taskid")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = self.call("SYNO.FileStation.DirSize", "status", taskid=taskid)
            if st.get("finished"):
                return {"files": st.get("num_file"), "dirs": st.get("num_dir"),
                        "bytes": st.get("total_size")}
            time.sleep(1.5)
        return None

    def homes_backup_roots(self, username):
        """List top-level folders under the user's Drive area, e.g.
        /homes/<user>/Drive — where Drive Client backup tasks usually land."""
        for base in (f"/homes/{username}/Drive", f"/homes/{username}"):
            try:
                data = self.call("SYNO.FileStation.List", "list",
                                 folder_path=base, limit=200)
                return base, [f["name"] for f in data.get("files", [])
                              if f.get("isdir") and not f["name"].startswith((".", "#"))]
            except Exception:
                continue
        return None, []


# ---------------------------------------------------------------------------
# DSM auth error codes worth explaining rather than printing raw.
AUTH_ERRORS = {
    400: "  <- wrong account or password",
    401: "  <- account disabled",
    402: "  <- permission denied; the service account must be in the "
         "administrators group",
    403: "  <- 2FA/OTP required; this collector cannot log in to an account "
         "with 2FA enabled",
    404: "  <- failed 2FA attempts, account temporarily blocked",
    407: "  <- blocked by DSM auto-block or the firewall; allow the "
         "container's source IP",
}


def pick(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def pick_int(d, keys):
    v = pick(d, keys)
    try:
        v = int(v)
        # some APIs return ms
        return v // 1000 if v > 10**12 else v
    except (TypeError, ValueError):
        return None


KNOWN_ROOTS = {"desktop", "documents", "downloads", "pictures", "music",
               "videos", "favorites", "appdata", "onedrive"}
# Structural path segments that are never a user's backup root.
GENERIC_SEGMENTS = {"backup", "users", "user", "drive", "home", "homes",
                    "volume1", "volume2", "mydrive", "team-folders"}


def infer_root(path, device=None):
    """Best-effort backup root from a logged path.

    Real layouts seen on Drive Server 3.1:
      /Backup/<device>/Users/<localuser>/Desktop/tax/2025.pdf   -> Desktop
      /homes/jsmith/Drive/DESKTOP-J5/Documents/a.txt            -> Documents

    Returns None rather than guessing when nothing looks like a real folder.
    An earlier version fell back to the first path segment, which produced
    roots like "Backup" and "BNO-SJohn-564-DQJ3k6h74g.local" — visibly wrong,
    and worse than showing nothing.
    """
    if not path:
        return None
    parts = [p for p in str(path).split("/") if p]
    if not parts:
        return None
    low = [p.lower() for p in parts]

    # /Backup/<device>/Users/<localuser>/<ROOT>/...
    if low[0] == "backup" and len(parts) >= 5 and low[2] == "users":
        return parts[4]

    for seg in parts:
        if seg.lower() in KNOWN_ROOTS:
            return seg

    # after .../Drive/<Computer>/<root>/...
    for i, seg in enumerate(low):
        if seg == "drive" and len(parts) > i + 2:
            cand = parts[i + 2]
            if cand.lower() not in GENERIC_SEGMENTS:
                return cand

    # Conservative fallback over directory segments only (drop the filename),
    # skipping structural names and anything hostname-shaped.
    for seg in parts[:-1]:
        s = seg.lower()
        if s in GENERIC_SEGMENTS:
            continue
        if device and seg == device:
            continue
        if "." in seg[1:]:            # e.g. BNO-SJohn-564-XXXX.local
            continue
        return seg
    return None


def status_for(last_success, now, has_footprint=True):
    """has_footprint = this user has some Drive presence (a client connection,
    or any log entry at all). Without it they simply do not use Drive, which
    is a different thing from a backup that stopped — and on a NAS where most
    accounts have no Drive client, lumping them together buries the handful of
    users who actually need attention."""
    if not last_success:
        return "never" if has_footprint else "unused"
    age_days = (now - last_success) / 86400
    if age_days <= 2:
        return "ok"
    if age_days <= 7:
        return "stale"
    return "failing"


def collect_nas(cfg, days, want_counts):
    pw = (cfg.get("password")
          or os.environ.get(cfg.get("password_env", ""), "")
          or "")
    ses = SynoSession(cfg["name"], cfg.get("host", "localhost"),
                      cfg.get("port", 5000), cfg.get("https", False),
                      cfg.get("verify_ssl", False), cfg["username"], pw,
                      base_url=cfg.get("base_url"))
    print(f"* Connecting to {cfg['name']} ({cfg.get('base_url') or cfg.get('host')}) ...")
    ses.connect()
    now = int(time.time())
    since = now - days * 86400
    try:
        admins = ses.admin_usernames()
        # Built-in and service accounts to hide. Extend per site with
        # SYNO_EXCLUDE_USERS="GuestAccount,kiosk" (case-insensitive).
        excluded = {"guest"}
        excluded |= {n.strip().lower()
                     for n in os.environ.get("SYNO_EXCLUDE_USERS", "").split(",")
                     if n.strip()}
        users = [u for u in ses.list_users()
                 if u.get("name") not in admins
                 and str(u.get("name", "")).lower() not in excluded]

        # Restrict to DSM groups, e.g. SYNO_INCLUDE_GROUPS="SynologyDriveUsers".
        # Keeps long-dead accounts out of the dashboard without touching DSM.
        include_groups = [g.strip() for g in
                          os.environ.get("SYNO_INCLUDE_GROUPS", "").split(",")
                          if g.strip()]
        if include_groups:
            allowed, resolved = set(), 0
            for g in include_groups:
                try:
                    members = ses.group_members(g)
                    allowed |= members
                    resolved += 1
                    print(f"  group '{g}': {len(members)} members")
                except Exception as e:
                    print(f"  ! Could not read group '{g}': {e}")
            if resolved:
                before = len(users)
                users = [u for u in users if u.get("name") in allowed]
                print(f"  restricted to {include_groups}: "
                      f"{len(users)} of {before} users")
                if not users:
                    # An empty dashboard reads as "nothing wrong", which is the
                    # most dangerous thing this tool could imply.
                    print("  ! The group filter matched NO users. The "
                          "dashboard will be empty — check the group name and "
                          "that it has members.")
            else:
                # Every group lookup failed — a typo'd name would otherwise
                # empty the dashboard and look like "all backups are fine".
                print("  ! No configured group could be read; showing ALL "
                      "users rather than an empty dashboard. Check "
                      "SYNO_INCLUDE_GROUPS.")
        print(f"  {len(users)} non-admin users, admins excluded: {sorted(admins)}")

        conns = ses.drive_connections()
        devices_by_user = defaultdict(list)
        for c in conns:
            uname = pick(c, F_USER)
            if not uname:
                continue
            devices_by_user[uname].append({
                "name": pick(c, F_DEVICE, default="unknown device"),
                "last_seen": pick_int(c, ["last_auth_time", "login_time",
                                          "last_connection_time", "last_seen",
                                          "connection_time", "time"]),
                # Real field is client_status: "on_line" / "off_line".
                "online": (str(pick(c, ["client_status"], "")).lower() == "on_line"
                           or bool(pick(c, ["online", "is_online", "connected"], False))),
                # "drive_backup" = a Drive Client backup task, "serversync" =
                # server-to-server sync. Worth surfacing: they answer
                # different questions about a user.
                "type": pick(c, ["client_type", "app"], ""),
            })

        log = ses.drive_log(since)
        print(f"  {len(log)} Drive log events in the last {days} days")
        daily = defaultdict(lambda: defaultdict(int))   # user -> date -> events
        roots = defaultdict(set)                        # user -> root folders
        client_types = defaultdict(set)                 # user -> drive_backup/...
        last_success = {}
        type_hist = defaultdict(int)                    # action code -> count
        seen_in_log = set()                             # any presence at all
        counted = skipped = orphaned = 0
        for item in log:
            uname = pick(item, F_USER)
            ts = pick_int(item, F_TIME)
            if not uname or not ts:
                orphaned += 1
                continue
            seen_in_log.add(uname)
            action = str(pick(item, F_ACTION, "")).lower()
            path = pick(item, F_PATH)
            if action in IGNORE_ACTIONS:
                continue

            # Drive Server reports the action as a numeric code (13 = file
            # event on 3.1), and the codes are undocumented and version-
            # specific. Rather than hardcode a code table that will rot, treat
            # the presence of a file path as the evidence that data actually
            # moved — which is the definition this tool works to anyway.
            # Verb-style actions, if a future version emits them, still use the
            # keyword match.
            if action.isdigit():
                counts_as_sync = bool(path)
            else:
                counts_as_sync = (not action) or any(a in action for a in SYNC_ACTIONS)

            type_hist[f"{action}{'' if path else ' (no path)'}"] += 1
            if not counts_as_sync:
                skipped += 1
                continue
            counted += 1

            day = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            daily[uname][day] += 1
            last_success[uname] = max(last_success.get(uname, 0), ts)
            r = infer_root(path, pick(item, F_DEVICE))
            if r:
                roots[uname].add(r)
            ct = pick(item, ["client_type"])
            if ct:
                client_types[uname].add(ct)

        print(f"  {counted} file events counted, {skipped} non-file events ignored"
              + (f", {orphaned} with no username/timestamp" if orphaned else ""))
        if type_hist:
            top = sorted(type_hist.items(), key=lambda kv: -kv[1])[:8]
            print("  action-code histogram (code -> events): "
                  + ", ".join(f"{k}={v}" for k, v in top))

        out_users = []
        for u in sorted(users, key=lambda x: x.get("name", "")):
            uname = u["name"]
            root_list = []
            base = None
            if not roots[uname]:
                base, listed = ses.homes_backup_roots(uname)
                for r in listed:
                    roots[uname].add(r)
            for r in sorted(roots[uname]):
                entry = {"name": r, "files": None}
                if want_counts:
                    p = f"{base or f'/homes/{uname}/Drive'}/{r}"
                    try:
                        stats = ses.dir_stats(p)
                        if stats:
                            entry["files"] = stats["files"]
                            entry["bytes"] = stats["bytes"]
                    except Exception:
                        pass
                root_list.append(entry)

            # Status comes from file events in the Drive log and nothing else.
            # Deliberately NO fallback to the client's last-connection time:
            # a Drive Client that is connected but has silently stopped syncing
            # is the exact failure this tool exists to catch, and using
            # last-seen here would report it as healthy. Device last-seen is
            # still carried in "devices" for display.
            ls = last_success.get(uname)
            has_footprint = bool(devices_by_user.get(uname)) or uname in seen_in_log
            out_users.append({
                "username": uname,
                "display_name": u.get("description") or uname,
                "devices": devices_by_user.get(uname, []),
                # Which kinds of Drive client this user actually runs, taken
                # from the log rather than the connection list: "drive_backup"
                # is a backup task, "serversync" is server-to-server sync.
                "client_types": sorted(client_types.get(uname, [])),
                "roots": root_list,
                "daily": dict(daily.get(uname, {})),
                "last_success": ls,
                "status": status_for(ls, now, has_footprint),
            })
        return {"name": cfg["name"], "host": cfg.get("host") or cfg.get("base_url", ""),
                "users": out_users}
    finally:
        ses.logout()


def discover(cfg):
    pw = (cfg.get("password")
          or os.environ.get(cfg.get("password_env", ""), "")
          or "")
    ses = SynoSession(cfg["name"], cfg.get("host", "localhost"),
                      cfg.get("port", 5000), cfg.get("https", False),
                      cfg.get("verify_ssl", False), cfg["username"], pw,
                      base_url=cfg.get("base_url"))
    ses.connect()
    try:
        print(f"\n=== {cfg['name']}: SYNO.SynologyDrive.* APIs exposed ===")
        found = False
        for name, info in sorted(ses.apis.items()):
            if name.startswith("SYNO.SynologyDrive"):
                found = True
                print(f"  {name}  v{info.get('minVersion')}-{info.get('maxVersion')}  ({info.get('path')})")
        if not found:
            print("  (none — is Synology Drive Server installed on this NAS?)")
        for api in CANDIDATE_LOG_APIS:
            if api in ses.apis:
                try:
                    data = ses.call(api, "list", offset=0, limit=1)
                    print(f"\n--- sample response from {api} ---")
                    print(json.dumps(data, indent=2)[:3000])
                    print("\nCompare the field names above with F_USER / F_TIME / "
                          "F_PATH / F_ACTION / F_DEVICE at the top of collector.py.")
                except Exception as e:
                    print(f"\n{api} list failed: {e}")
    finally:
        ses.logout()


# ---------------------------------------------------------------------------
def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_password():
    path = os.environ.get("SYNO_PASS_FILE")
    if path:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError as e:
            sys.exit(f"SYNO_PASS_FILE set but unreadable: {e}")
    return os.environ.get("SYNO_PASS", "")


def config_from_env():
    """Single-NAS config built from environment variables — the container path."""
    user = os.environ.get("SYNO_USER")
    if not user:
        sys.exit("No config.json found and SYNO_USER is not set. "
                 "Set SYNO_USER/SYNO_PASS (see README) or provide a config file.")
    pw = _env_password()
    if not pw:
        sys.exit("SYNO_USER is set but no password given "
                 "(set SYNO_PASS or SYNO_PASS_FILE).")
    nas = {
        "name": os.environ.get("SYNO_NAS_NAME") or socket.gethostname(),
        "host": os.environ.get("SYNO_HOST", "localhost"),
        "port": int(os.environ.get("SYNO_PORT", "5000")),
        "https": _env_bool("SYNO_HTTPS", False),
        "verify_ssl": _env_bool("SYNO_VERIFY_SSL", False),
        "username": user,
        "password": pw,
    }
    if os.environ.get("SYNO_BASE_URL"):
        nas["base_url"] = os.environ["SYNO_BASE_URL"]
    return {
        "days": int(os.environ.get("SYNO_DAYS", "90")),
        "output": os.environ.get("SYNO_OUTPUT", "web/data.json"),
        "file_counts": _env_bool("SYNO_FILE_COUNTS", False),
        "nases": [nas],
    }


def load_config(path):
    if path and os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        if not cfg.get("nases"):
            sys.exit(f"{path} has no 'nases' entries.")
        return cfg
    return config_from_env()


def run_once(cfg, days, output, want_counts):
    result = {"generated_at": int(time.time()), "days": days, "nases": []}
    ok = True
    for nas in cfg["nases"]:
        try:
            result["nases"].append(collect_nas(nas, days, want_counts))
        except Exception as e:
            ok = False
            print(f"! FAILED {nas.get('name')}: {e}")
            result["nases"].append({"name": nas.get("name"),
                                    "host": nas.get("host"),
                                    "error": str(e), "users": []})
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    # Write via a temp file so the dashboard never fetches a half-written JSON.
    tmp = output + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=1)
    os.replace(tmp, output)
    print(f"Wrote {output}")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Synology Drive backup status collector")
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument("-o", "--output", default=None,
                    help="override output path (default from config/env, else web/data.json)")
    ap.add_argument("--days", type=int, default=None, help="history window")
    ap.add_argument("--file-counts", action="store_true",
                    help="also compute per-root file counts (slower)")
    ap.add_argument("--discover", action="store_true",
                    help="print available Drive APIs + a sample log entry, then exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    days = args.days or cfg.get("days", 90)
    output = args.output or cfg.get("output", "web/data.json")
    want_counts = args.file_counts or cfg.get("file_counts", False)

    if args.discover:
        for nas in cfg["nases"]:
            discover(nas)
        return

    sys.exit(0 if run_once(cfg, days, output, want_counts) else 1)


if __name__ == "__main__":
    main()
