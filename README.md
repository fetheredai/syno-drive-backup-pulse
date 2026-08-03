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

The container collects immediately on start, then every 4 hours. The
dashboard's **Rescan** button re-reads the Drive log on demand — it starts the
collection, then polls until it finishes and reloads, so the numbers on screen
are never stale-but-looking-fresh. A rescan cannot overlap the scheduled run.

From a shell:

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
2. Copy `deploy.sh` to the NAS, e.g. `/volume1/docker/backup-pulse/`.
3. SSH in and run it:

```
cd /volume1/docker/backup-pulse
sudo ./deploy.sh
```

The first run asks for the client name, the service account, its password,
which groups to show, and whether to require sign-in. It then pulls the image,
replaces any previous container, starts the new one with host networking and a
restart policy, waits for the first collection and tells you whether it
actually succeeded.

Those answers are remembered, so **later runs need no input at all**:

```
sudo ./deploy.sh                 # update this site, no prompts
sudo ./deploy.sh --reconfigure   # change the saved answers
sudo ./deploy.sh --show          # current health, no redeploy
```

The password is stored in a root-only `0600` file beside the script and
mounted read-only into the container, so `docker inspect` shows a path rather
than the password. Non-secret answers live in `.pulse-config` next to it.
`SECRET_MODE=env` passes the password as an environment variable instead.

Upgrading DSM to 7.2+ gets you Container Manager and the compose workflow
above, which is nicer to operate. `deploy.sh` keeps working either way, so
that upgrade can happen on the client's schedule rather than yours.

## Where the credentials live

DSM has no API-token or app-password concept for these Web API endpoints, so a
username and password is the only way in. Collection has to keep running
unattended across reboots, so something has to hold that credential. There are
three options, in increasing order of paranoia:

**Prompted once, stored in a root-only file (the `deploy.sh` default).** The
password goes to a `0600` file beside the script, bind-mounted read-only into
the container with `SYNO_PASS_FILE` pointing at it — so `docker inspect` shows
a path rather than the password. Because it persists, re-deploying and
updating need no re-entry.

**Prompted, passed as an environment variable.** Run
`sudo SECRET_MODE=env ./deploy.sh`. Nothing is written to disk by the script,
but the value lands in Docker's own container config and is visible to root
via `docker inspect` — which is where every other Synology container keeps its
passwords, so this matches normal practice on the platform. It also means
re-entering the password on every deploy.

**A `.env` file.** Convenient for unattended re-deploys across many sites, at
the cost of a plaintext credential on disk that you manage. If you use one,
`chmod 600` it and keep it off cloud-synced storage — a client's NAS admin
password should not be syncing to Dropbox.

Whichever you choose, limit the blast radius at the DSM end: the service
account needs the administrators group, but you can still restrict it by
source IP under Control Panel > Security > Firewall.

## Signing in

The dashboard requires a login by default, and verifies it against DSM itself:
staff sign in with the Synology account they already have. There is no second
password store to maintain, and removing someone's DSM account removes their
access here too.

Restrict it further with `SYNO_LOGIN_GROUP=DriveViewers` — only members of that
DSM group can sign in. Membership is checked with the service account and
fails closed: if the group cannot be read, access is denied rather than
granted.

Two caveats. An account with 2FA enabled cannot sign in, because the collector
has no OTP handling; the login page says so explicitly rather than claiming a
bad password. And this server speaks plain HTTP, so on anything less trusted
than a VPN put a reverse proxy with TLS in front of it — the session cookie is
`HttpOnly` and `SameSite=Lax`, but it is not encrypted in transit.

Repeated failures are throttled per account, because hammering DSM's login
endpoint can trip its auto-block and lock out the source address.

Set `SYNO_AUTH=false` to turn login off entirely on a trusted network.

## Choosing which users appear

By default every non-admin DSM account shows up, which on an established NAS
means a lot of dormant accounts. Two controls:

`SYNO_INCLUDE_GROUPS=SynologyDriveUsers` limits the dashboard to members of one
or more DSM groups (comma-separated). Create the group in DSM, put the people
who should be backing up in it, and the dashboard follows. If a configured
group cannot be read at all, the collector shows everyone and says so rather
than silently presenting an empty dashboard — an empty dashboard reads as
"nothing is wrong", which is the worst thing this tool could imply.

`SYNO_EXCLUDE_USERS=GuestAccount,kiosk` drops specific accounts by name.

Group membership marks users rather than removing them, so the dashboard has
an **Active / All users** toggle: Active (the default) shows the configured
group, All shows everyone collected. The toggle hides itself when no group is
configured, since it would do nothing. Set `SYNO_STRICT_GROUPS=true` if you
would rather non-members were never collected at all — the toggle then has
nothing extra to show.

Users are also classified: an account with no Drive client at all is `unused`
rather than `never`, and the dashboard hides those behind a chip. `never` is
reserved for someone who has a client but has never actually backed anything
up — which is worth investigating.

## Configuration

Everything is environment variables. `deploy.sh` asks for the essentials once
and remembers them; the rest have defaults. They can also be set in an
optional `.env`:

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
| `SYNO_AUTH` | `true` | Require sign-in to view the dashboard |
| `SYNO_LOGIN_GROUP` | — | Only this DSM group may sign in |
| `SYNO_SESSION_HOURS` | `12` | How long a session lasts |
| `SYNO_INCLUDE_GROUPS` | — | Members of these DSM groups are "active" |
| `SYNO_STRICT_GROUPS` | `false` | Drop non-members entirely instead of marking them |
| `SYNO_EXCLUDE_USERS` | — | Hide these accounts by name |
| `SYNO_MAX_LOG_PAGES` | `500` | Log paging cap; raise for very busy servers |

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
