#!/usr/bin/env python3
"""
Drive Server 4.0: find the page size that works, and the query that returns
USER backup activity.

probe4 established that target=all&share_type=all is accepted on 4.0 — the
same parameters 3.1 uses. Two things remain unexplained:

  1. The collector's limit=1000 returns "401 failed to get user" while
     limit=3 succeeds. Some rows cannot resolve a user, and one of them
     appears to fail the whole page.
  2. What comes back is team-folder traffic (client_type "system", empty
     username, share_name "Creative"), and total=19 — far too little for a
     NAS with 20 connected backup clients. The per-user backup events must
     be reachable some other way.

So this probe:
  * sweeps limit to find the largest page size that does not fail,
  * checks whether failures are positional (a specific offset) rather than
    size-related, by walking with a small limit,
  * tries share_type / target variants and reports, for each, how many rows
    carry a real username and what client_types appear,
  * shows a sample row that has a username, which is the one we need.

    sudo docker exec -i backup-pulse python3 - < probe5.py
"""

import json
import os
import sys

sys.path.insert(0, "/app")
import collector  # noqa: E402

LIMITS = [3, 10, 25, 50, 100, 200, 500, 1000]
VARIANTS = [
    {"target": "all", "share_type": "all"},
    {"target": "all", "share_type": "0"},
    {"target": "all", "share_type": "1"},
    {"target": "all", "share_type": "2"},
    {"target": "user", "share_type": "all"},
    {"target": "user", "share_type": "0"},
    {"target": "mydrive", "share_type": "all"},
    {"target": "home", "share_type": "all"},
    {"target": "all", "share_type": "mydrive"},
    {"target": "all", "share_type": "user"},
]


def connect():
    pw = os.environ.get("SYNO_PASS", "")
    if not pw and os.environ.get("SYNO_PASS_FILE"):
        with open(os.environ["SYNO_PASS_FILE"]) as f:
            pw = f.read().strip()
    ses = collector.SynoSession(
        "probe5", os.environ.get("SYNO_HOST", "localhost"),
        int(os.environ.get("SYNO_PORT", "5000")),
        os.environ.get("SYNO_HTTPS", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        os.environ.get("SYNO_USER"), pw)
    ses.connect()
    return ses


def call(ses, **kw):
    info = ses.apis.get("SYNO.SynologyDrive.Log")
    p = {"api": "SYNO.SynologyDrive.Log", "method": "list",
         "version": info.get("maxVersion", 1), "_sid": ses.sid}
    p.update(kw)
    try:
        return ses.s.get(f"{ses.base}/{info['path']}", params=p,
                         verify=ses.verify, timeout=60).json()
    except Exception as e:
        return {"success": False, "error": {"code": -3, "msg": repr(e)}}


def err(resp):
    e = resp.get("error") or {}
    errs = e.get("errors")
    if isinstance(errs, dict):
        return f"{e.get('code')}:{errs.get('message') or errs.get('reason')}"
    return str(e.get("code"))


def summarise(items):
    named = [i for i in items if i.get("username")]
    types = {}
    targets = {}
    for i in items:
        types[i.get("client_type")] = types.get(i.get("client_type"), 0) + 1
        targets[str(i.get("target"))[:20]] = \
            targets.get(str(i.get("target"))[:20], 0) + 1
    return named, types, targets


def main():
    ses = connect()
    print("connected OK\n")
    try:
        base = {"target": "all", "share_type": "all"}

        # --- 1. how big a page can we ask for? ---------------------------
        print("=== page size sweep (target=all, share_type=all, offset=0) ===")
        best = 0
        for lim in LIMITS:
            r = call(ses, offset=0, limit=lim, **base)
            if r.get("success"):
                items = (r.get("data") or {}).get("items", [])
                total = (r.get("data") or {}).get("total")
                print(f"  limit={lim:<5} OK   returned={len(items):<5} total={total}")
                best = lim
            else:
                print(f"  limit={lim:<5} FAIL {err(r)}")
        print(f"\n  largest working page size: {best}")

        # --- 2. is the failure positional? -------------------------------
        print("\n=== walking with a safe page size, looking for a poison row ===")
        step = max(1, min(best, 25))
        offset, seen, bad_offsets, all_items = 0, 0, [], []
        for _ in range(200):
            r = call(ses, offset=offset, limit=step, **base)
            if not r.get("success"):
                bad_offsets.append((offset, err(r)))
                if step == 1:
                    offset += 1          # step over the unreadable row
                    continue
                # narrow down: retry this window one row at a time
                for o in range(offset, offset + step):
                    r1 = call(ses, offset=o, limit=1, **base)
                    if r1.get("success"):
                        all_items.extend((r1.get("data") or {}).get("items", []))
                    else:
                        bad_offsets.append((o, err(r1)))
                offset += step
                continue
            items = (r.get("data") or {}).get("items", [])
            total = (r.get("data") or {}).get("total")
            all_items.extend(items)
            seen += len(items)
            offset += len(items) or step
            if not items or (total is not None and offset >= total):
                break
        print(f"  walked {seen} rows; {len(bad_offsets)} unreadable")
        if bad_offsets:
            print(f"  first few bad offsets: {bad_offsets[:5]}")

        named, types, targets = summarise(all_items)
        print(f"  rows with a username: {len(named)} / {len(all_items)}")
        print(f"  client_type breakdown: {types}")
        print(f"  target breakdown: {targets}")
        if named:
            print("\n----- a row that HAS a username -----")
            print(json.dumps(named[0], indent=2, ensure_ascii=False)[:1200])

        # --- 3. which query surfaces user backups? -----------------------
        print("\n=== variants: which returns rows with real usernames? ===")
        for v in VARIANTS:
            r = call(ses, offset=0, limit=min(best or 25, 50), **v)
            label = f"target={v['target']!r} share_type={v['share_type']!r}"
            if not r.get("success"):
                print(f"  {label:<46} FAIL {err(r)}")
                continue
            data = r.get("data") or {}
            items = data.get("items", [])
            n, t, _ = summarise(items)
            print(f"  {label:<46} OK total={data.get('total')} "
                  f"rows={len(items)} named={len(n)} types={t}")
            if n and not named:
                print("    ----- sample with username -----")
                print(json.dumps(n[0], indent=2, ensure_ascii=False)[:900])
    finally:
        ses.logout()


if __name__ == "__main__":
    main()
