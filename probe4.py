#!/usr/bin/env python3
"""
Probe the Drive Server 4.0 log API.

Round three found that 4.0 rejects `target` and `share_type` with reason
"type" — the parameters want a different DATA TYPE, not a different value, so
searching more string values is pointless. Some combinations then reached
`401 failed to get user`, which suggests the query must be scoped to a user.

So this probe varies three things the earlier ones did not:

  * ENCODING — raw, JSON-quoted, JSON array, integer. DSM parameters are
    sometimes JSON-encoded (`additional=["email"]`), and a plain string then
    fails the type check.
  * API VERSION — 4.0 may expose a newer version with different requirements.
  * USER SCOPE — a real username taken from the client list, tried under
    several parameter names.

It reports which combination produced which error, rather than only counting
errors, so a near-miss is visible.

    sudo docker exec -i backup-pulse python3 - < probe4.py
"""

import itertools
import json
import os
import sys

sys.path.insert(0, "/app")
import collector  # noqa: E402

MAX_TRIALS = 1200
TARGETS = ["all", "user", "global", "team_folder", "mydrive", "personal"]
SHARE_STR = ["all", "mydrive", "team_folder", "personal", "user"]
SHARE_INT = [0, 1, 2, 3, -1]
USER_PARAMS = ["user", "username", "user_name", "owner", "account", "uid"]


def enc_variants(value):
    """(label, encoded) forms DSM might expect for one parameter value."""
    out = [("raw", str(value))]
    if isinstance(value, str):
        out.append(("json_str", json.dumps(value)))
        out.append(("json_arr", json.dumps([value])))
    else:
        out.append(("json_arr", json.dumps([value])))
    return out


def connect():
    pw = os.environ.get("SYNO_PASS", "")
    if not pw and os.environ.get("SYNO_PASS_FILE"):
        with open(os.environ["SYNO_PASS_FILE"]) as f:
            pw = f.read().strip()
    ses = collector.SynoSession(
        "probe4", os.environ.get("SYNO_HOST", "localhost"),
        int(os.environ.get("SYNO_PORT", "5000")),
        os.environ.get("SYNO_HTTPS", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_USER"), pw)
    ses.connect()
    return ses


def raw(ses, api, method, version, **kw):
    info = ses.apis.get(api)
    if not info:
        return {"success": False, "error": {"code": -1, "msg": "api absent"}}
    p = {"api": api, "method": method, "version": version, "_sid": ses.sid}
    p.update(kw)
    try:
        return ses.s.get(f"{ses.base}/{info['path']}", params=p,
                         verify=ses.verify, timeout=30).json()
    except Exception as e:
        return {"success": False, "error": {"code": -3, "msg": repr(e)}}


def reason_of(resp):
    """Short label for what DSM objected to."""
    err = resp.get("error") or {}
    errs = err.get("errors")
    if isinstance(errs, dict) and errs.get("name"):
        return f"{errs['name']}:{errs.get('reason')}"
    if isinstance(errs, dict) and errs.get("message"):
        return f"{err.get('code')}:{errs['message']}"
    return str(err.get("code"))


def find_list(data):
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return k, v
    return None, None


def main():
    ses = connect()
    print("connected OK\n")
    trials = 0
    try:
        # --- what does this version actually expose? ---------------------
        print("=== SYNO.SynologyDrive.* APIs (log-related) ===")
        for name, info in sorted(ses.apis.items()):
            if name.startswith("SYNO.SynologyDrive") and \
                    any(w in name.lower() for w in ("log", "event", "audit",
                                                    "activity", "history",
                                                    "record", "report")):
                print(f"  {name}  v{info.get('minVersion')}-{info.get('maxVersion')}")
        info = ses.apis.get("SYNO.SynologyDrive.Log", {})
        vmin = info.get("minVersion", 1)
        vmax = info.get("maxVersion", 1)
        print(f"\nSYNO.SynologyDrive.Log versions {vmin}-{vmax}")

        # --- a real username to scope by ---------------------------------
        r = raw(ses, "SYNO.SynologyDrive.Connection", "list",
                info.get("maxVersion", 1), offset=0, limit=5)
        conns = (r.get("data") or {}).get("items", [])
        sample_user = next((c.get("client_name") for c in conns
                            if c.get("client_name")), None)
        me = os.environ.get("SYNO_USER")
        print(f"scoping candidates: {sample_user!r} (a real Drive user), "
              f"{me!r} (the service account)")

        # --- phase A: get past the type checks ---------------------------
        print("\n=== phase A: encodings for target / share_type ===")
        promising, seen = [], {}
        share_values = SHARE_STR + SHARE_INT
        for version in range(vmax, vmin - 1, -1):
            for t_val, s_val in itertools.product(TARGETS, share_values):
                for (t_lbl, t_enc), (s_lbl, s_enc) in itertools.product(
                        enc_variants(t_val), enc_variants(s_val)):
                    if trials >= MAX_TRIALS:
                        break
                    resp = raw(ses, "SYNO.SynologyDrive.Log", "list", version,
                               offset=0, limit=3, target=t_enc, share_type=s_enc)
                    trials += 1
                    if resp.get("success"):
                        print(f"\n  *** SUCCESS v{version} "
                              f"target={t_enc!r}({t_lbl}) "
                              f"share_type={s_enc!r}({s_lbl}) ***")
                        data = resp.get("data", resp)
                        key, items = find_list(data)
                        if items:
                            print(f"  list '{key}', ITEM KEYS: "
                                  f"{sorted(items[0].keys())}")
                        print(json.dumps(data, indent=2)[:2500])
                        return
                    why = reason_of(resp)
                    seen[why] = seen.get(why, 0) + 1
                    # Anything that is not a type/condition complaint means the
                    # encoding was accepted and DSM moved on to a real check.
                    if "type" not in why and "condition" not in why:
                        promising.append((version, t_enc, t_lbl, s_enc, s_lbl, why))
        print(f"  {trials} attempts; error tally: {json.dumps(seen, indent=2)}")

        if not promising:
            print("\n  Nothing got past the type checks.")
            return

        print(f"\n  {len(promising)} combination(s) passed the type checks, e.g.:")
        for p in promising[:6]:
            print(f"    v{p[0]} target={p[1]!r}({p[2]}) "
                  f"share_type={p[3]!r}({p[4]}) -> {p[5]}")

        # --- phase B: scope the query to a user --------------------------
        print("\n=== phase B: adding a user parameter ===")
        base = promising[0]
        version, t_enc, s_enc = base[0], base[1], base[3]
        for uname in [u for u in (sample_user, me) if u]:
            for pname in USER_PARAMS:
                for u_lbl, u_enc in enc_variants(uname):
                    if trials >= MAX_TRIALS:
                        break
                    kw = {"offset": 0, "limit": 3, "target": t_enc,
                          "share_type": s_enc, pname: u_enc}
                    resp = raw(ses, "SYNO.SynologyDrive.Log", "list",
                               version, **kw)
                    trials += 1
                    if resp.get("success"):
                        print(f"\n  *** SUCCESS with {pname}={u_enc!r} "
                              f"({u_lbl}), user {uname!r} ***")
                        print(f"  full parameters: "
                              f"{json.dumps({k: v for k, v in kw.items()})}")
                        data = resp.get("data", resp)
                        key, items = find_list(data)
                        if items:
                            print(f"  list '{key}', ITEM KEYS: "
                                  f"{sorted(items[0].keys())}")
                        print(json.dumps(data, indent=2)[:2500])
                        return
        print(f"  no user parameter worked ({trials} attempts total).")
        print("  Last errors seen in phase B suggest checking Drive Admin "
              "Console > Log in a browser with DevTools open: the Network tab "
              "shows exactly what the console itself sends on this version.")
    finally:
        ses.logout()


if __name__ == "__main__":
    main()
