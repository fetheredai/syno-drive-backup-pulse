# Installing at a new client site

Per-NAS runbook. Works on DSM 7.0 through 7.3+, with either the legacy Docker
package or Container Manager — `deploy.sh` uses the docker CLI, which both
provide, so the steps are identical.

Budget about 15 minutes, most of it waiting for the first collection.

---

## 1. Create the service account (DSM)

Control Panel > User & Group > User > Create.

- Name: `svc-drivemonitor`
- Must be in the **administrators** group. Drive Admin Console data is
  admin-only and nothing works without this.
- **2FA must be off** for this account. The collector has no OTP handling and
  login will fail outright. If the client enforces 2FA org-wide, this account
  needs an exemption.
- Optional hardening: Control Panel > Security > Firewall, restrict it by
  source IP.

Note the password somewhere safe — you enter it once, during step 5.

## 2. Check Drive log retention (DSM)

Synology Drive Admin Console > Settings. Log retention must be at least as
long as the history window you want (90 days by default). The heatmap can only
show what the NAS still keeps; shorter retention silently truncates the
calendar.

## 3. Enable SSH (DSM)

Control Panel > Terminal & SNMP > **Enable SSH service**.

You can turn it back off after step 5 — the container keeps running. You will
need it again for updates, or use Container Manager's UI to pull.

## 4. Open the dashboard port, if the firewall is on (DSM)

Control Panel > Security > Firewall. Allow TCP **8477** from the management
network. Skip if the firewall is disabled.

## 5. Install (SSH)

The account you SSH in with must be in the administrators group, or `sudo`
will not work. That is separate from `svc-drivemonitor`.

```bash
ssh <admin-user>@<nas-ip>
sudo mkdir -p /volume1/docker/backup-pulse
cd /volume1/docker/backup-pulse
sudo curl -fsSL -o deploy.sh \
  https://raw.githubusercontent.com/fetheredai/syno-drive-backup-pulse/main/deploy.sh
wc -c deploy.sh            # sanity check: should be ~11000, never 0
sudo chmod +x deploy.sh
sudo ./deploy.sh
```

`-f` on curl matters: without it a proxy error page or a 404 gets written into
the file and the script silently does nothing.

It asks for:

| Prompt | Answer |
|---|---|
| Client / NAS name | The client name — this is the dashboard heading |
| DSM service account | `svc-drivemonitor` |
| Groups to include | Blank, or a DSM group of the users who should be backing up |
| Require sign-in | `Y` |
| Group allowed to sign in | Blank for any DSM account, or a group |
| Password | The service account password, entered twice |

Then it pulls the image, starts the container, and waits for the first
collection. Expect `OK — first collection succeeded`.

## 6. Verify it actually parsed the log

**This is the step that matters.** "Collection succeeded" only means nothing
threw — it does not mean the log was understood. Different Drive Server
versions use different log fields, and a mismatch produces a dashboard where
everyone reads `never`, which looks like a client with no backups at all.

```bash
sudo docker exec backup-pulse python3 -c "
import json
from collections import Counter
d=json.load(open('/app/web/data.json'))
n=d['nases'][0]
us=n.get('users',[])
print('users:',len(us),' statuses:',dict(Counter(u['status'] for u in us)))
act=[u for u in us if u['daily']]
print('users with activity:',len(act))
for u in sorted(act,key=lambda x:-len(x['daily']))[:10]:
    print(f\"  {u['username']:16} {u['status']:8} days={len(u['daily']):3} roots={[r['name'] for r in u['roots']][:4]}\")
"
```

You want a realistic spread — some `ok`, maybe a `stale` or `failing`, and
non-zero `days=` for people who really are backing up.

If **everything** is `never` or `unused` while the NAS has connected Drive
clients, the collector now says so explicitly in its log. Confirm with:

```bash
sudo docker logs backup-pulse 2>&1 | tail -20
```

A `! WARNING: no user shows any file activity` line means the field mappings
need checking for this Drive Server version — go to "Different Drive Server
version" below.

Then open `http://<nas-ip>:8477/` and sign in with a DSM account.

## 7. Hand over

- The dashboard URL and that it needs a DSM login.
- It refreshes every 4 hours; the **Rescan** button re-reads on demand.
- Worst-first ordering: whoever is at the top needs attention.
- `unused` means no Drive client at all, and is hidden behind a chip.

---

## Updating a site later

```bash
cd /volume1/docker/backup-pulse
sudo ./deploy.sh
```

No prompts — the answers and password are saved in `.pulse-config` and
`.pulse-secret` beside the script. `--reconfigure` changes them,
`--show` prints current health without redeploying.

## Different Drive Server version

Field names and required parameters are undocumented and vary by version. Ours
are confirmed on Drive Server 3.1. If step 6 came up empty:

```bash
cd /volume1/docker/backup-pulse
sudo curl -fsSL -o probe2.py \
  https://raw.githubusercontent.com/fetheredai/syno-drive-backup-pulse/main/probe2.py
sudo docker exec -i backup-pulse python3 - < probe2.py
```

`probe2.py` grid-searches the log parameters and dumps a real sample item.
Compare its field names with the `F_*` lists at the top of `collector.py`.
See the "What the real API turned out to be" section in `HANDOFF.md` for what
3.1 uses and why each mapping is the way it is.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Script runs, no output at all | `deploy.sh` is 0 bytes — the download failed. Re-run the curl with `-f`. |
| `No docker binary found` | Docker / Container Manager package not installed or not started. |
| `Found the docker binary ... could not talk to the daemon` | Run with `sudo`. |
| First collection FAILED, code 400 | Wrong service-account password. `sudo ./deploy.sh --reconfigure` |
| First collection FAILED, code 402 | Account is not in the administrators group. |
| First collection FAILED, code 403 | 2FA is enabled on the service account. |
| First collection FAILED, code 407 | DSM auto-block or firewall rejecting the container. |
| Sign-in says "Could not verify group membership" | The service account cannot read the group — check the container log for a `CONFIG PROBLEM` line at startup. |
| Everyone reads `never` | Field-mapping mismatch. See above. |
| Dashboard unreachable | Firewall: allow TCP 8477. Check nothing else uses that port; `SYNO_WEB_PORT=9477 sudo ./deploy.sh` moves it. |
