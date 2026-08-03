#!/usr/bin/env python3
"""
Round-two probe: grid-search the Drive Log parameters.

Round one established that SYNO.SynologyDrive.Log wants `target`, and then
`share_type`, and that generic values ("all", "0", "1") do not satisfy
share_type. This version:

  * searches the cartesian product of curated candidate values,
  * extends the search automatically if DSM reveals a further parameter,
  * always reports what it accumulated and every distinct error it saw, even
    on failure (round one hid that, which cost a round trip),
  * dumps the other APIs worth considering as data sources,
  * checks whether user Drive folders are actually listable.

    sudo docker exec -i backup-pulse python3 - < probe2.py
"""

import itertools
import json
import os
import sys
import time

sys.path.insert(0, "/app")
import collector  # noqa: E402

NOW = int(time.time())

VALUES = {
    "target": ["all", "user", "users", "global", "server", "team_folder",
               "mydrive", "my_drive", "personal", "shared", "home",
               "0", "1", "2", "-1"],
    "share_type": ["mydrive", "my_drive", "team_folder", "teamfolder",
                   "personal", "shared_with_me", "shared", "all", "user",
                   "home", "labels", "0", "1", "2", "3", "-1"],
    "path": ["/", "", "/mydrive", "/team-folders"],
    "date_from": ["0"],
    "date_to": [str(NOW)],
    "action": ["all"],
    "keyword": [""],
    "sort_by": ["time"],
    "sort_direction": ["DESC"],
    "user_name": [""],
    "username": [""],
}
DEFAULT_VALUES = ["all", "0", "1", "", "-1", "true"]
MAX_TRIALS = 600


def connect():
    pw = os.environ.get("SYNO_PASS", "")
    if not pw and os.environ.get("SYNO_PASS_FILE"):
        with open(os.environ["SYNO_PASS_FILE"]) as f:
            pw = f.read().strip()
    ses = collector.SynoSession(
        "probe", os.environ.get("SYNO_HOST", "localhost"),
        int(os.environ.get("SYNO_PORT", "5000")),
        os.environ.get("SYNO_HTTPS", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_USER"), pw)
    ses.connect()
    return ses


def raw(ses, api, method, version=None, **kw):
    info = ses.apis.get(api)
    if not info:
        return {"success": False, "error": {"code": -1, "msg": "api not present"}}
    p = {"api": api, "method": method,
         "version": version or info.get("maxVersion", 1), "_sid": ses.sid}
    p.update(kw)
    try:
        r = ses.s.get(f"{ses.base}/{info['path']}", params=p,
                      verify=ses.verify, timeout=30)
        return r.json()
    except Exception as e:
        return {"success": False, "error": {"code": -3, "msg": repr(e)}}


def err_param(resp):
    """(name, reason) that DSM is complaining about, if any."""
    errs = (resp.get("error") or {}).get("errors")
    if isinstance(errs, dict) and errs.get("name"):
        return errs["name"], errs.get("reason")
    if isinstance(errs, list):
        for e in errs:
            if isinstance(e, dict) and e.get("name"):
                return e["name"], e.get("reason")
    return None, None


def grid_solve(ses, api, method, base):
    """Product-search candidate values, growing the parameter set as DSM asks."""
    names, trials, seen_errors = [], 0, {}

    probe_resp = raw(ses, api, method, **base)
    if probe_resp.get("success"):
        return base, probe_resp, names, seen_errors
    n, _ = err_param(probe_resp)
    if n:
        names.append(n)

    while trials < MAX_TRIALS:
        if not names:
            return None, probe_resp, names, seen_errors
        grids = [VALUES.get(nm, DEFAULT_VALUES) for nm in names]
        restart = False
        for combo in itertools.product(*grids):
            if trials >= MAX_TRIALS:
                break
            params = dict(base)
            params.update(dict(zip(names, combo)))
            resp = raw(ses, api, method, **params)
            trials += 1
            if resp.get("success"):
                return params, resp, names, seen_errors
            nm, reason = err_param(resp)
            key = f"{nm}:{reason}" if nm else json.dumps(resp.get("error"))
            seen_errors[key] = seen_errors.get(key, 0) + 1
            if nm and nm not in names:
                names.append(nm)      # a new requirement appeared
                restart = True
                break
        if not restart:
            return None, None, names, seen_errors
    return None, None, names, seen_errors


def find_list(data):
    if not isinstance(data, dict):
        return None, None
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return k, v
    return None, None


def show(title, obj, limit=2500):
    print(f"\n----- {title} -----")
    t = json.dumps(obj, indent=2, ensure_ascii=False)
    print(t[:limit] + ("\n... (truncated)" if len(t) > limit else ""))


def main():
    ses = connect()
    print("connected OK\n")
    try:
        print("=== grid-searching SYNO.SynologyDrive.Log ===")
        params, resp, names, errors = grid_solve(
            ses, "SYNO.SynologyDrive.Log", "list", {"offset": 0, "limit": 5})
        print(f"  parameters DSM asked for, in order: {names}")
        if params:
            extra = {k: v for k, v in params.items() if k not in ("offset", "limit")}
            print(f"  *** WORKING PARAMETERS: {json.dumps(extra)} ***")
            data = resp.get("data", resp)
            key, items = find_list(data)
            if items:
                print(f"  list under '{key}', {len(items)} item(s)")
                print(f"  ITEM KEYS: {sorted(items[0].keys())}")
            show("sample log response", data)
        else:
            print("  no combination succeeded.")
            print(f"  distinct errors seen: {json.dumps(errors, indent=2)}")

        print("\n=== other candidate sources ===")
        for api, methods in [
            ("SYNO.SynologyDrive.Statistics", ["list", "get", "info"]),
            ("SYNO.SynologyDrive.Dashboard", ["list", "info", "get_info"]),
            ("SYNO.SynologyDrive.Files", ["list"]),
            ("SYNO.SynologyDrive.Info", ["get", "list"]),
        ]:
            for m in methods:
                r = raw(ses, api, m, offset=0, limit=3)
                if r.get("success"):
                    data = r.get("data", r)
                    key, items = find_list(data)
                    print(f"\n  {api}.{m}: OK"
                          + (f"  list '{key}', keys={sorted(items[0].keys())}"
                             if items else ""))
                    show(f"{api}.{m}", data, 900)
                    break
                nm, reason = err_param(r)
                code = (r.get("error") or {}).get("code")
                print(f"  {api}.{m}: code={code}"
                      + (f" needs '{nm}' ({reason})" if nm else ""))

        print("\n=== can we list a real user's Drive folder? ===")
        r = raw(ses, "SYNO.FileStation.List", "list", folder_path="/homes", limit=6)
        users = [f.get("name") for f in (r.get("data") or {}).get("files", [])
                 if f.get("isdir")]
        print(f"  /homes contains: {users[:6]}")
        for u in users[:3]:
            for sub in (f"/homes/{u}/Drive", f"/homes/{u}"):
                rr = raw(ses, "SYNO.FileStation.List", "list",
                         folder_path=sub, limit=10)
                if rr.get("success"):
                    names_ = [f.get("name") for f in
                              (rr.get("data") or {}).get("files", [])]
                    print(f"  {sub}: OK -> {names_[:8]}")
                    break
                else:
                    print(f"  {sub}: {json.dumps(rr.get('error'))}")

        print("\n=== full Connection item (for field mapping) ===")
        r = raw(ses, "SYNO.SynologyDrive.Connection", "list", offset=0, limit=200)
        items = (r.get("data") or {}).get("items", [])
        print(f"  {len(items)} connections")
        types = {}
        for it in items:
            types[it.get("client_type")] = types.get(it.get("client_type"), 0) + 1
        print(f"  client_type breakdown: {types}")
        if items:
            show("one connection item", items[0], 1200)
    finally:
        ses.logout()


if __name__ == "__main__":
    main()
