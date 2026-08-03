# Synology Drive Backup Pulse

A dashboard for MSPs that answers one question fast: **which users are actually
backing up with Synology Drive Client, and which have silently stopped?**

Synology Drive Admin Console will happily show a client as "connected" long
after it has stopped syncing anything. The only ground truth is the Drive
Server log. This container turns that log into a per-user backup health view:
status (OK / Stale / Failing / Never), a 90-day calendar heatmap of backup
activity per user, the root folders each user backs up, and device/last-seen
info.

It runs as one container per NAS, on the NAS it monitors. Deploy the same image
to every client site.

## What it looks like

Users are sorted worst-first — Failing, then Never, then Stale, then OK — so
the people who need attention are at the top. Each row is one user; each column
is one day; darker cells mean more files synced that day. Long empty stretches
on the right of a row are a backup that stopped.

## Install on a NAS

**1. Create a service account.** In DSM, Control Panel > User & Group, create
an account (e.g. `svc-drivemonitor`). It must be in the **administrators**
group, because Drive Admin Console data is admin-only. Do not enable 2FA on it
— this collector has no OTP handling and login will fail. Restrict it under
Control Panel > Security > Firewall if you want belt and braces.

**2. Check log retention.** Drive Admin Console > Settings, make sure log
retention is at least as long as the history window you want (default 90 days).
The heatmap can only show what the NAS still has.

**3. Create the project.** In Container Manager > Project > Create, point it at
a folder containing `docker-compose.yml` and a `.env` file. Copy
`.env.example` to `.env` and fill in at minimum:

```
PULSE_IMAGE=ghcr.io/<your-github-user>/syno-drive-backup-pulse:latest
SYNO_NAS_NAME=Acme Co - DS923+
SYNO_USER=svc-drivemonitor
SYNO_PASS=your-password
```

Container Manager pulls the image straight from GHCR on build. You do **not**
need to add ghcr.io under Container Manager's Registry tab — that path is
known to fail with "registry returned bad result", because Container Manager
expects Docker Hub-style search endpoints that GHCR does not implement.
Referencing the full `ghcr.io/...` path in the compose file bypasses the
registry UI entirely and works.

**4. Open the dashboard** at `http://<nas-ip>:8477/`.

The container collects immediately on start, then every 4 hours. To force a
collection without waiting:

```
docker exec backup-pulse python3 collector.py
```

### If your DSM has "Docker" instead of "Container Manager"

Container Manager arrived in DSM 7.2. On DSM 7.0 and 7.1 the package is still
called Docker, and it has no Project tab — so there is no GUI for compose.
Adding ghcr.io under that package's Registry tab does not work either, for the
same reason it fails in Container Manager. On those versions, deploy over SSH
with `deploy.sh`, which uses plain `docker` and needs no compose at all:

1. Enable SSH: Control Panel > Terminal & SNMP > Enable SSH service.
2. Create a folder on the NAS, e.g. `/volume1/docker/backup-pulse/`, and put
   `deploy.sh` and your filled-in `.env` in it.
3. SSH in and run:

```
cd /volume1/docker/backup-pulse
sudo ./deploy.sh
```

It pulls the image, removes any previous container, starts the new one with
host networking and a restart policy, and prints the dashboard URL. Re-run the
same command to update that site later — `.env` is the only state.

Upgrading DSM to 7.2+ gets you Container Manager and the compose workflow
above, which is nicer to operate. `deploy.sh` keeps working either way, so
that upgrade can happen on the client's schedule rather than yours.

## Configuration

Everything is environment variables, set in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `SYNO_NAS_NAME` | container hostname | Heading shown in the dashboard — use the client name |
| `SYNO_USER` | — | Service account (required) |
| `SYNO_PASS` | — | Service account password (required) |
| `SYNO_PASS_FILE` | — | Read the password from a file instead, for Docker secrets |
| `SYNO_HOST` | `localhost` | DSM host |
| `SYNO_PORT` | `5000` | DSM port |
| `SYNO_HTTPS` | `false` | Use HTTPS |
| `SYNO_VERIFY_SSL` | `false` | Verify the certificate |
| `SYNO_BASE_URL` | — | Full URL, overrides host/port/https |
| `SYNO_DAYS` | `90` | History window |
| `SYNO_INTERVAL_HOURS` | `4` | How often to collect |
| `SYNO_FILE_COUNTS` | `false` | Count files per root folder (slow) |
| `SYNO_WEB_PORT` | `8477` | Dashboard port |

### Why the defaults talk to localhost over plain HTTP

The container runs on the NAS it monitors, with `network_mode: host`, so
`localhost:5000` never leaves the box. That sidesteps self-signed certificates,
firewall rules and QuickConnect relays entirely. If you would rather run a
central collector that reaches several NASes over the network, use
`SYNO_BASE_URL` (or `config.json` — see below) and set `SYNO_VERIFY_SSL`
appropriately.

### Monitoring several NASes from one container

Mount a `config.json` (see `config.example.json`) at `/app/config.json`. If
that file exists it takes precedence over the environment variables, and every
NAS listed gets collected into one dashboard with a NAS picker. This is the
older central-collector mode; one container per NAS is simpler to deploy and
keeps each client's credentials on that client's own hardware.

## Health and troubleshooting

`http://<nas-ip>:8477/healthz` returns JSON with the last run time, whether it
succeeded, the last error, and when the next run is due. The container's
Docker healthcheck uses the same endpoint.

If the dashboard shows a yellow banner saying the data is days old, the
collector has stopped even though the web server is still up — check the
container log in Container Manager.

Common login failures are reported with an explanation rather than a bare DSM
error code. Code 402 means the account is not an administrator; 403 means 2FA
is enabled on it; 407 means DSM auto-block or the firewall is rejecting the
container's source IP.

## How it decides a backup happened

A user counts as having backed up on a given day if the Drive Server log shows
at least one **file event** from them that day — upload, edit, create, rename
and similar. Login, logout, browse and preview events are ignored.

Connection status is deliberately not trusted, and there is no fallback to the
client's last-seen time. A Drive Client that is connected but has silently
stopped syncing is the exact failure this tool exists to catch; using last-seen
would report it as healthy. That behaviour is pinned by a test
(`test_connected_but_never_syncing_reads_as_never`).

| Status | Meaning |
|---|---|
| OK | file events within the last 2 days |
| Stale | last activity 3–7 days ago |
| Failing | nothing for more than 7 days |
| Never | no Drive file activity seen at all |

Thresholds live in `status_for()` in `collector.py`. Tune them to the client's
SLA — a daily-backup client probably wants OK to mean within 24 hours.

## Publishing from GitHub

Pushing to `main` runs the test suite and, if it passes, builds a multi-arch
image (`linux/amd64` + `linux/arm64`) and publishes it to GitHub Container
Registry. Both architectures matter: Synology "plus" models are Intel/AMD, the
value models are ARM.

First push:

```
git remote add origin git@github.com:<your-github-user>/syno-drive-backup-pulse.git
git push -u origin main
```

Then, once only, make the published package public so client NASes can pull it
without credentials: the repo's Packages section > the package > Package
settings > Change visibility > Public. Until you do this the package is private
and every NAS would need `docker login ghcr.io` with a `read:packages` token.

The Actions run summary prints the exact `PULSE_IMAGE=` line to paste into each
site's `.env`.

Tag a release to cut versioned tags:

```
git tag v1.0.0 && git push --tags     # publishes :1.0.0, :1.0, :1 and :latest
```

Pinning each client to a version tag rather than `:latest` is worth doing once
this is in production — it makes "which build is that site running?" answerable.

`build.sh` still exists for building locally on a Mac with Docker Desktop when
you want to iterate without pushing.

## Updating a client site

Container Manager > Project > select the project > Build (or Action > Pull
image, depending on DSM version). That pulls the current image and recreates
the container. Updates are deliberately per-site and manual, so a bad push
cannot roll itself out everywhere.

If you later want hands-off updates, a Watchtower container per NAS will pull
on a schedule. Be aware it triggers a DSM "container stopped unexpectedly"
notification each time it updates something.

## Tests

```
python3 -m unittest tests -v
```

`mock_dsm.py` stands up a fake DSM Web API — API discovery, login, paged user
lists, a paged Drive log with realistic per-user activity gaps, File Station
listings — and the tests run the real collector against it. That covers login,
log pagination, field picking, day bucketing, admin exclusion, root inference
and the status thresholds without touching hardware.

What the tests do **not** prove is that a real Drive Server uses the field
names in `F_USER` / `F_TIME` / `F_PATH` / `F_ACTION` / `F_DEVICE`. See below.

## Not yet validated against real hardware

The `SYNO.SynologyDrive.*` APIs are undocumented and field names vary between
Drive Server versions. Before trusting the output on a new site, run:

```
docker exec backup-pulse python3 collector.py --discover
```

That prints every `SYNO.SynologyDrive.*` API the NAS exposes plus a sample log
entry. Compare the field names in that sample with the `F_*` lists at the top
of `collector.py` and add or reorder as needed. If discovery is ambiguous,
open Drive Admin Console with browser DevTools on the Network tab and watch
what the Log and Client List pages actually call.

Then sanity-check one known-active user against what Admin Console shows.

## Notes and limits

Only non-admin users appear; the `administrators` group and `guest` are
excluded automatically. Root folders are inferred from logged file paths, and
fall back to listing the user's `/homes/<user>/Drive` area — the dashboard
never shows individual filenames, only root names and counts. File counts use
File Station `DirSize`, which walks folders server-side and is slow on large
datasets, hence off by default.

The dashboard lists usernames and hostnames, so keep it behind the VPN rather
than exposing port 8477 publicly. It pulls its webfont from Google Fonts and
falls back to system fonts cleanly on isolated networks.

Collection rebuilds from the live log each run, so history is capped by the
NAS's log retention. A SQLite store that accumulates across runs would lift
that; it is not built yet.
