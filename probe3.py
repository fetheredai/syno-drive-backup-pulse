#!/usr/bin/env python3
"""
Diagnose DSM group lookups.

"Could not verify group membership" means SYNO.Core.Group.Member raised, not
that the account is absent from the group. This tries the call several ways —
with and without paging parameters, at different API versions — and lists the
groups DSM actually reports, so a name or case mismatch is obvious.

    sudo docker exec -i backup-pulse python3 - < probe3.py
    sudo docker exec -i backup-pulse python3 - < probe3.py MyGroupName
"""

import json
import os
import sys

sys.path.insert(0, "/app")
import collector  # noqa: E402


def connect():
    pw = os.environ.get("SYNO_PASS", "")
    if not pw and os.environ.get("SYNO_PASS_FILE"):
        with open(os.environ["SYNO_PASS_FILE"]) as f:
            pw = f.read().strip()
    ses = collector.SynoSession(
        "probe3", os.environ.get("SYNO_HOST", "localhost"),
        int(os.environ.get("SYNO_PORT", "5000")),
        os.environ.get("SYNO_HTTPS", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_USER"), pw)
    ses.connect()
    return ses


def raw(ses, api, method, version=None, **kw):
    info = ses.apis.get(api)
    if not info:
        return {"success": False, "error": {"code": -1, "msg": "api absent"}}
    p = {"api": api, "method": method,
         "version": version or info.get("maxVersion", 1), "_sid": ses.sid}
    p.update(kw)
    try:
        return ses.s.get(f"{ses.base}/{info['path']}", params=p,
                         verify=ses.verify, timeout=30).json()
    except Exception as e:
        return {"success": False, "error": {"code": -3, "msg": repr(e)}}


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else \
        os.environ.get("SYNO_LOGIN_GROUP") or \
        os.environ.get("SYNO_INCLUDE_GROUPS", "").split(",")[0].strip()

    ses = connect()
    print(f"connected as {os.environ.get('SYNO_USER')}\n")
    try:
        info = ses.apis.get("SYNO.Core.Group.Member", {})
        print(f"SYNO.Core.Group.Member versions: "
              f"{info.get('minVersion')}-{info.get('maxVersion')}\n")

        print("=== groups DSM reports ===")
        for m in ("list", "get"):
            r = raw(ses, "SYNO.Core.Group", m, offset=0, limit=200)
            if r.get("success"):
                groups = [g.get("name") for g in
                          (r.get("data") or {}).get("groups", [])]
                print(f"  via {m}: {groups}")
                if target and target not in groups:
                    near = [g for g in groups
                            if g and g.lower() == str(target).lower()]
                    print(f"  !! '{target}' is not in that list."
                          + (f" Case mismatch? DSM has {near}." if near else ""))
                break
            print(f"  {m}: {json.dumps(r.get('error'))}")

        if not target:
            print("\nNo group to test. Pass one as an argument.")
            return

        print(f"\n=== reading members of '{target}' ===")
        variants = [
            ("plain (no paging)", dict(group=target)),
            ("with offset/limit", dict(group=target, offset=0, limit=500)),
            ("version 1, plain", dict(group=target, version=1)),
            ("param 'name' not 'group'", dict(name=target)),
        ]
        for label, kw in variants:
            r = raw(ses, "SYNO.Core.Group.Member", "list", **kw)
            if r.get("success"):
                data = r.get("data") or {}
                users = [u.get("name") for u in data.get("users", [])]
                print(f"  OK   {label}: total={data.get('total')} "
                      f"members={users[:12]}"
                      + (" ..." if len(users) > 12 else ""))
            else:
                print(f"  FAIL {label}: {json.dumps(r.get('error'))}")

        print("\n=== what collector.group_members() does now ===")
        try:
            print(f"  -> {sorted(ses.group_members(target))[:15]}")
        except Exception as e:
            print(f"  raised: {e!r}")
    finally:
        ses.logout()


if __name__ == "__main__":
    main()
