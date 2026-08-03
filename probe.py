#!/usr/bin/env python3
"""
Adaptive probe for the Synology Drive Admin Console APIs.

SYNO.SynologyDrive.* is undocumented and its required parameters vary between
Drive Server versions. Usefully, DSM's error 120 names the parameter it wants:

    {"code": 120, "errors": {"name": "target", "reason": "required"}}

So rather than guessing blind, this walks the error messages: call, read which
parameter is missing, try candidate values for it, and repeat until the call
succeeds or nothing works. Then it dumps a sample response so the F_* field
mappings in collector.py can be set from real data.

Run inside the container, which already has requests and the credentials:

    sudo docker exec -i backup-pulse python3 - < probe.py
"""

import json
import os
import sys
import time

sys.path.insert(0, "/app")
import collector  # noqa: E402

MAX_STEPS = 8
SAMPLE_CHARS = 2500

# Values to try for a parameter DSM says is required. Ordered most-likely
# first. UNKNOWN is the fallback for a parameter name we have not seen before.
CANDIDATES = {
    "target": ["all", "user", "users", "team_folder", "teamfolder", "sync",
               "file", "files", "drive", "server", "client", "admin",
               "home", "mydrive", "0", "1", "-1"],
    "type": ["all", "0", "1", "-1"],
    "log_type": ["all", "0", "1"],
    "category": ["all", "0", "1"],
    "action": ["all", "0", "-1"],
    "path": ["/", ""],
    "keyword": [""],
    "sort_by": ["time", "log_time"],
    "sort_direction": ["DESC", "desc"],
    "date_from": ["0"],
    "date_to": [str(int(time.time()))],
    "start": ["0"],
    "get_all": ["true", "1"],
}
UNKNOWN = ["all", "0", "1", "", "-1", "true"]


def connect():
    pw = os.environ.get("SYNO_PASS", "")
    if not pw and os.environ.get("SYNO_PASS_FILE"):
        with open(os.environ["SYNO_PASS_FILE"]) as f:
            pw = f.read().strip()
    ses = collector.SynoSession(
        "probe",
        os.environ.get("SYNO_HOST", "localhost"),
        int(os.environ.get("SYNO_PORT", "5000")),
        os.environ.get("SYNO_HTTPS", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_USER"),
        pw,
    )
    ses.connect()
    return ses


def raw(ses, api, method, version=None, **kw):
    """Call without raising, so we can read the error payload."""
    info = ses.apis.get(api)
    if not info:
        return {"success": False, "error": {"code": -1, "msg": "api not present"}}
    params = {"api": api, "method": method,
              "version": version or info.get("maxVersion", 1),
              "_sid": ses.sid}
    params.update(kw)
    try:
        r = ses.s.get(f"{ses.base}/{info['path']}", params=params,
                      verify=ses.verify, timeout=30)
        return r.json()
    except ValueError:
        return {"success": False, "error": {"code": -2, "msg": r.text[:200]}}
    except Exception as e:
        return {"success": False, "error": {"code": -3, "msg": repr(e)}}


def missing_param(resp):
    """Return the parameter name DSM says is required, if it says so."""
    err = resp.get("error") or {}
    errs = err.get("errors")
    if isinstance(errs, dict):
        if errs.get("reason") in ("required", "not_found", None) and errs.get("name"):
            return errs["name"]
    if isinstance(errs, list):
        for e in errs:
            if isinstance(e, dict) and e.get("name"):
                return e["name"]
    return None


def solve(ses, api, method, base_params):
    """Walk DSM's error messages until the call succeeds."""
    params = dict(base_params)
    tried = []
    for _ in range(MAX_STEPS):
        resp = raw(ses, api, method, **params)
        if resp.get("success"):
            return params, resp
        name = missing_param(resp)
        if not name:
            return None, resp
        if name in tried:
            return None, resp
        tried.append(name)
        progressed = False
        for val in CANDIDATES.get(name, UNKNOWN):
            trial = dict(params)
            trial[name] = val
            r2 = raw(ses, api, method, **trial)
            if r2.get("success"):
                return trial, r2
            n2 = missing_param(r2)
            # A different parameter name means this value was accepted and DSM
            # moved on to the next thing it wants.
            if n2 and n2 != name:
                params = trial
                progressed = True
                break
        if not progressed:
            return None, resp
    return None, {"success": False, "error": {"msg": "gave up"}}


def show(title, obj, limit=SAMPLE_CHARS):
    print(f"\n----- {title} -----")
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    print(text[:limit] + ("\n... (truncated)" if len(text) > limit else ""))


def describe_items(data):
    """Find the list inside a response and report its item keys."""
    if not isinstance(data, dict):
        return
    for key, val in data.items():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            print(f"\n  list is under key: '{key}'  ({len(val)} item(s) returned)")
            print(f"  item keys: {sorted(val[0].keys())}")
            return key, val
    print(f"\n  no list of objects found; top-level keys: {sorted(data.keys())}")
    return None, None


def main():
    ses = connect()
    print("connected OK")
    try:
        # --- the log, which is the whole point --------------------------
        print("\n=== SYNO.SynologyDrive.Log ===")
        params, resp = solve(ses, "SYNO.SynologyDrive.Log", "list",
                             {"offset": 0, "limit": 3})
        if params:
            extra = {k: v for k, v in params.items()
                     if k not in ("offset", "limit")}
            print(f"  WORKING PARAMETERS: {json.dumps(extra)}")
            data = resp.get("data", resp)
            describe_items(data)
            show("sample log response", data)
        else:
            print(f"  could not make it succeed. Last error: "
                  f"{json.dumps(resp.get('error'))}")
            print("  Trying method 'get' instead of 'list'...")
            p2, r2 = solve(ses, "SYNO.SynologyDrive.Log", "get",
                           {"offset": 0, "limit": 3})
            if p2:
                print(f"  WORKING with method=get, params: {json.dumps(p2)}")
                describe_items(r2.get("data", r2))
                show("sample (method=get)", r2.get("data", r2))

        # --- everything else worth seeing -------------------------------
        for api, method, base in [
            ("SYNO.SynologyDrive.Connection", "list", {"offset": 0, "limit": 3}),
            ("SYNO.SynologyDrive.Dashboard", "get", {}),
            ("SYNO.SynologyDrive.Statistics", "get", {}),
            ("SYNO.SynologyDrive.Users", "list", {"offset": 0, "limit": 3}),
        ]:
            print(f"\n=== {api} ({method}) ===")
            p, r = solve(ses, api, method, base)
            if p:
                extra = {k: v for k, v in p.items() if k not in ("offset", "limit")}
                if extra:
                    print(f"  params needed: {json.dumps(extra)}")
                data = r.get("data", r)
                describe_items(data)
                show(f"sample {api}", data, 1200)
            else:
                print(f"  failed: {json.dumps(r.get('error'))}")

        # --- where do user homes actually live? -------------------------
        print("\n=== File Station: locating user Drive folders ===")
        for path in ("/homes", "/home"):
            r = raw(ses, "SYNO.FileStation.List", "list",
                    folder_path=path, limit=5)
            ok = r.get("success")
            print(f"  {path}: {'OK' if ok else json.dumps(r.get('error'))}")
            if ok:
                files = (r.get("data") or {}).get("files", [])
                print(f"    entries: {[f.get('name') for f in files][:5]}")
    finally:
        ses.logout()


if __name__ == "__main__":
    main()
