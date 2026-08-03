# HANDOFF — Synology Drive Backup Pulse

Start a session with: "Read HANDOFF.md and pick up where it leaves off."

## Goal

MSP tool to monitor whether end users' Synology Drive Client backups are
actually running. The Drive Admin Console shows clients as "connected" even
when they've silently stopped syncing; the only ground truth is the Drive
Server log. This project turns that log into a per-user backup health
dashboard: status (OK / Stale / Failing / Never), a 90-day calendar heatmap of
backup activity per user, the root folders each user backs up (names and counts
only, never individual files), and device/last-seen info. Non-admin users only.

**Deployment shape (decided):** one container per NAS, running on the NAS it
monitors, deployed as a reusable image to any client site. Not a central
collector. The multi-NAS code path still exists via `config.json` but is the
secondary mode.

## Current state

**Live on the pilot NAS (BNO-Rackstation, DSM 7.0.1, Atom C3538).** The Drive
API mappings are validated against real hardware; mock_dsm.py reproduces the
real payloads. First live run found one failing and one stale backup among 5
users with Drive clients, out of 80 DSM accounts.

Has login (DSM-backed), DSM-group filtering, and zero-prompt redeploys.

## Files

- `collector.py` — Python 3.8+, needs `requests`. Logs into DSM, pulls users
  (excludes administrators group + guest), the Drive client list and the Drive
  Server log; aggregates file events per user per day; infers backup roots from
  logged paths (fallback: lists `/homes/<user>/Drive`); optional per-root file
  counts via File Station DirSize. Config comes from environment variables, or
  from `config.json` if that file exists. Writes `data.json` atomically.
- `app.py` — container entrypoint. One process: a background thread running
  collect-then-sleep on `SYNO_INTERVAL_HOURS`, plus a threaded HTTP server for
  `web/` with a `/healthz` endpoint and `no-store` on `data.json`.
- `web/dashboard.html` — single static file, vanilla JS, no build step. Status
  filter chips, user search, shared-axis heatmap wall, hover tooltips. Falls
  back to embedded sample data if `data.json` is missing, so it demos offline.
- `Dockerfile` / `docker-compose.yml` / `.env.example` / `build.sh` — the
  container packaging. Multi-arch amd64 + arm64. The compose file takes the
  image path from `PULSE_IMAGE` in `.env` rather than hardcoding an owner.
- `deploy.sh` — SSH deployment for DSM 7.0/7.1, which have the legacy Docker
  package with no Project tab and therefore no GUI compose. Plain `docker run`,
  no compose dependency. Also the update mechanism on those sites. Saves
  answers to `.pulse-config` and the password to a 0600 `.pulse-secret`, so
  re-running is a zero-prompt update; `--reconfigure` changes them.
- `auth.py` — dashboard login, verified against DSM itself so there is no
  second password store. `SYNO_LOGIN_GROUP` restricts to a DSM group.
- `probe.py` / `probe2.py` — the API probers that cracked the Drive log
  parameters. Keep them: they are how you diagnose a new Drive Server version.
- `.github/workflows/docker-publish.yml` — CI. Push to `main` runs the tests,
  then builds and publishes `ghcr.io/<owner>/<repo>` for both architectures.
  Tags `v*` cut semver tags. Pull requests test but never publish.
- `mock_dsm.py` / `tests.py` — fake DSM Web API and the end-to-end test suite.
- `README.md` — install, configuration, troubleshooting, thresholds, limits.
- `config.example.json` — optional, central-collector mode only.

## Key design decisions

1. "Successful backup day" = ≥1 file event (upload/edit/create/etc.) from that
   user in the Drive log that day. Login/logout/browse/preview are ignored.
2. **Connection status is never used for health.** There is deliberately no
   fallback to the client's last-seen time when the log is empty. An earlier
   version did fall back, which reported a connected-but-never-syncing client
   as OK — precisely the silent failure the tool exists to catch. Pinned by
   `test_connected_but_never_syncing_reads_as_never`.
3. Status thresholds in `status_for()`: OK ≤2 days, Stale 3–7, Failing >7,
   Never = no activity ever. Tune per client SLA.
4. Container talks to DSM on `localhost:5000` over plain HTTP via
   `network_mode: host`. That traffic never leaves the NAS, which avoids
   certificate handling, firewall rules and QuickConnect relays entirely.
5. No volume is mounted over `/app/web`. A named volume there would shadow the
   image's `dashboard.html` and keep serving the old one after an image update.
   `data.json` is rebuilt every run, so nothing needs persisting.
6. Dashboard stays dependency-free static HTML. Must stay behind VPN — it
   exposes usernames and hostnames.

## What the real API turned out to be

Recorded here because none of it is documented and all of it was surprising:

- `SYNO.SynologyDrive.Log` returns error 120 unless given **`target=all` and
  `share_type=all`**. DSM names the missing parameter in the error, which is
  how probe2.py found them by search rather than guesswork.
- The file path is **`s1`**; the client device is **`s2`**.
- There is a key literally named **`target`** holding `"user"` — it is not a
  path, and any field-name search that includes it will silently produce
  garbage roots.
- The action is a **numeric `type`** (13, 15, 23, 12 all carry paths on 3.1;
  34 and 10 do not). The codes are undocumented, so the collector treats "has
  a file path" as the evidence data moved rather than hardcoding a code table.
- On `Connection`, **`client_name` is the USER and `client_id` is the DEVICE**.
  `client_type` distinguishes `drive_backup` from `serversync` and `drive`.
- Real backup paths are `/Backup/<device>/Users/<localuser>/<Root>/...`.
- File Station **cannot list other users' homes** even as an admin (408), so
  root inference must come from logged paths.

## Validating a new site or a different Drive Server version

The mappings above are confirmed on Drive Server 3.1. They are undocumented
and can change between versions, so when a new site returns everything as
`never`, or roots look wrong, re-run the probers rather than guessing:

```
docker exec -i backup-pulse python3 - < probe2.py
```

`probe2.py` grid-searches the Log parameters, growing the set as DSM reveals
further requirements, and always reports what it accumulated plus every
distinct error — including on failure. It also dumps Connection, Statistics,
Info, and checks whether user Drive folders are listable. `probe.py` is the
simpler adaptive version. Feed the sample item's field names into the `F_*`
lists at the top of `collector.py`.

If the API shape ever moves out of reach entirely, the fallbacks are Drive's
PostgreSQL database on the NAS, or forwarding events via Log Center syslog.

Two things still unverified:

- `--file-counts` has never been run on a real dataset. It uses File Station
  `DirSize`, and since File Station cannot list other users' homes here (408),
  the path construction likely needs rework using `filestation_link_prefix`
  from the log. Leave it off until then.
- ~3,500 of 199,659 log events on the pilot had no username or timestamp and
  were dropped. The collector now reports the count; worth confirming they are
  team-folder or system events and not user activity being discarded.

## Prerequisites per NAS

- Service account (e.g. `svc-drivemonitor`) in the administrators group (Drive
  Admin Console data is admin-only), **no 2FA** (no OTP handling), ideally
  source-IP restricted via DSM firewall.
- DSM 7.2+ with Container Manager for the compose workflow, **or** DSM 7.0/7.1
  with the legacy Docker package plus SSH, using `deploy.sh`. The pilot site
  (Atom C3538 / Denverton, x86_64) is on DSM 7.0.1 and takes the `deploy.sh`
  path. Note the legacy Docker package has no Project tab and cannot add
  ghcr.io as a registry, so SSH is not optional there.
- Drive Admin Console log retention ≥ the history window, or the calendar is
  truncated.

## Next steps

1. Confirm whether sjohn and kbonner are genuinely broken backups or just
   people who were away — the first real judgement call the tool has produced.
2. Roll out to remaining client sites: copy `deploy.sh`, run it, answer the
   prompts once.
3. Pin sites to a version tag rather than `:latest` so "which build is that
   site on?" is answerable.
4. Per-client SLA thresholds — still hardcoded in `status_for()`.
5. Consider the settings UI for group selection (currently config-only).

## Backlog / not yet built

- SQLite history store so the heatmap can exceed the NAS's log retention
  (collector rebuilds from the live log each run).
- Email/webhook alert when a user transitions to Failing (ties into RMM/PSA).
- Config-driven per-client SLA thresholds.
- OTP support for service accounts with 2FA.
- Distinguish Drive "backup task" activity from ordinary sync-folder activity
  if the log's action/category field turns out to differentiate them — check
  during the `--discover` step.
- Optional auth in front of the dashboard; today it relies on network position.
