#!/usr/bin/env python3
"""
Dashboard login, backed by DSM itself.

Staff sign in with the Synology account they already have: the entered
credentials are verified by attempting a real DSM login, so there is no second
password store to maintain and no separate offboarding step. Optionally
restrict access to members of a DSM group.

Notes and limits:
  * An account with 2FA enabled cannot log in this way — the collector has no
    OTP handling. Put such users in an exempt group or use a service account.
  * Failed attempts are throttled here, because hammering DSM's login endpoint
    can trip its auto-block and lock out the source address.
  * Sessions live in memory, so restarting the container logs everyone out.
    That is the right trade-off for a status dashboard.
"""

import os
import threading
import time

import collector

SESSION_COOKIE = "pulse_session"
SESSION_HOURS = float(os.environ.get("SYNO_SESSION_HOURS", "12"))
LOGIN_GROUP = os.environ.get("SYNO_LOGIN_GROUP", "").strip()

# Throttle: this many failures for one account, then a cooldown.
MAX_FAILS = 5
COOLDOWN_S = 300
GROUP_CACHE_S = 300

_lock = threading.Lock()
_sessions = {}      # token -> {"user": str, "exp": float}
_fails = {}         # username -> [count, first_fail_ts]
_group_cache = {}   # group -> (members, fetched_at)


def _session_kwargs():
    return dict(
        host=os.environ.get("SYNO_HOST", "localhost"),
        port=int(os.environ.get("SYNO_PORT", "5000")),
        https=os.environ.get("SYNO_HTTPS", "false").lower() in ("1", "true", "yes"),
        verify_ssl=os.environ.get("SYNO_VERIFY_SSL", "false").lower()
        in ("1", "true", "yes"),
    )


def service_password():
    """The service account's password, or raise saying exactly what is wrong.

    This used to swallow every error and return "", which DSM answered with
    'wrong account or password' — sending you to check credentials that were
    fine. The common cause is Docker's bind-mount behaviour: if the source
    file does not exist when the container starts, Docker creates a DIRECTORY
    at the mount point, and reading it fails.
    """
    pw = os.environ.get("SYNO_PASS", "")
    if pw:
        return pw
    path = os.environ.get("SYNO_PASS_FILE")
    if not path:
        raise RuntimeError(
            "Neither SYNO_PASS nor SYNO_PASS_FILE is set, so the service "
            "account has no password to log in with.")
    if os.path.isdir(path):
        raise RuntimeError(
            f"{path} is a directory, not a file. Docker creates one when the "
            f"bind-mount source is missing. Re-run deploy.sh so the secret "
            f"file exists before the container starts.")
    try:
        with open(path) as f:
            pw = f.read().strip()
    except OSError as e:
        raise RuntimeError(f"Cannot read {path}: {e}") from e
    if not pw:
        raise RuntimeError(f"{path} is empty, so there is no password to use.")
    return pw


# Kept for callers that predate the rename.
_service_password = service_password


def group_members(group):
    """Members of a DSM group, read with the service account and cached."""
    now = time.time()
    with _lock:
        hit = _group_cache.get(group)
        if hit and now - hit[1] < GROUP_CACHE_S:
            return hit[0]
    ses = collector.SynoSession(
        "auth", username=os.environ.get("SYNO_USER"),
        password=service_password(), **_session_kwargs())
    ses.connect()
    try:
        members = ses.group_members(group)
    finally:
        ses.logout()
    with _lock:
        _group_cache[group] = (members, now)
    return members


def _throttled(username):
    with _lock:
        rec = _fails.get(username)
        if not rec:
            return 0
        count, first = rec
        if time.time() - first > COOLDOWN_S:
            _fails.pop(username, None)
            return 0
        if count >= MAX_FAILS:
            return int(COOLDOWN_S - (time.time() - first))
    return 0


def _record_fail(username):
    with _lock:
        count, first = _fails.get(username, (0, time.time()))
        if time.time() - first > COOLDOWN_S:
            count, first = 0, time.time()
        _fails[username] = (count + 1, first)


def _clear_fails(username):
    with _lock:
        _fails.pop(username, None)


def check_credentials(username, password):
    """(ok, message). Verified by a real DSM login."""
    username = (username or "").strip()
    if not username or not password:
        return False, "Enter a username and password."

    wait = _throttled(username)
    if wait:
        return False, f"Too many failed attempts. Try again in {wait}s."

    ses = collector.SynoSession(
        "login", username=username, password=password, **_session_kwargs())
    try:
        ses.connect()
    except Exception as e:
        _record_fail(username)
        text = str(e)
        if "403" in text or "2FA" in text or "OTP" in text:
            return False, ("This account has 2-factor authentication enabled, "
                           "which this dashboard cannot handle.")
        return False, "Incorrect username or password."
    finally:
        try:
            ses.logout()
        except Exception:
            pass

    if LOGIN_GROUP:
        try:
            if username not in group_members(LOGIN_GROUP):
                _record_fail(username)
                return False, f"Your account is not a member of {LOGIN_GROUP}."
        except Exception as e:
            # Fail closed: if we cannot confirm membership we must not grant
            # access, or the group restriction is decorative.
            print(f"[auth] group check failed for {LOGIN_GROUP}: {e}", flush=True)
            return False, "Could not verify group membership. Try again later."

    _clear_fails(username)
    return True, ""


def new_session(username):
    import secrets
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {"user": username,
                            "exp": time.time() + SESSION_HOURS * 3600}
    return token


def session_user(token):
    if not token:
        return None
    with _lock:
        s = _sessions.get(token)
        if not s:
            return None
        if s["exp"] < time.time():
            _sessions.pop(token, None)
            return None
        return s["user"]


def end_session(token):
    with _lock:
        _sessions.pop(token, None)


LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in — Drive Backup Pulse</title><style>
:root{--bg:#F2F4F6;--panel:#fff;--ink:#16232E;--muted:#5C6B78;--line:#DDE3E8;
--accent:#22679C;--failing:#C6413B;
--sans:system-ui,-apple-system,"Segoe UI",sans-serif;--mono:ui-monospace,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:28px;width:100%;max-width:360px}
h1{margin:0 0 4px;font-size:18px}
.sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:20px}
label{display:block;font-size:12px;font-weight:600;margin:14px 0 5px}
input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:4px;
font-size:14px;font-family:var(--sans);background:#fff;color:var(--ink)}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:20px;padding:10px;border:0;border-radius:4px;
background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
.err{background:#FCEDEC;border:1px solid #E9C3C0;color:var(--failing);
border-radius:4px;padding:9px 11px;font-size:13px;margin-top:16px}
.note{font-size:11px;color:var(--muted);margin-top:18px;line-height:1.5}
</style></head><body>
<form class="card" method="POST" action="/login">
  <h1>Drive Backup Pulse</h1>
  <div class="sub">__NAS__</div>
  __ERROR__
  <label for="u">Synology username</label>
  <input id="u" name="username" autocomplete="username" autofocus required>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
  <div class="note">Use your Synology DSM account.__GROUPNOTE__</div>
</form></body></html>"""


def login_page(error=""):
    nas = os.environ.get("SYNO_NAS_NAME", "")
    note = (f" Access is limited to members of {LOGIN_GROUP}."
            if LOGIN_GROUP else "")
    html = LOGIN_PAGE.replace("__NAS__", _escape(nas))
    html = html.replace("__GROUPNOTE__", _escape(note))
    html = html.replace("__ERROR__",
                        f'<div class="err">{_escape(error)}</div>' if error else "")
    return html.encode()


def _escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
