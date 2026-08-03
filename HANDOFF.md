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

Packaged and tested against a mock DSM. **Still not validated against real
Drive Server hardware** — see "Outstanding" below.

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

## Outstanding — validate before trusting output

The `SYNO.SynologyDrive.*` APIs are undocumented and field names vary between
Drive Server versions. `collector.py` has candidate constants at the top:
`CANDIDATE_CONNECTION_APIS`, `CANDIDATE_LOG_APIS`, and field-name lists
`F_USER`, `F_TIME`, `F_PATH`, `F_ACTION`, `F_DEVICE`. These are educated
guesses with defensive parsing, exercised only against `mock_dsm.py`.

Validation loop, on the first real NAS:

1. Deploy the container, then
   `docker exec backup-pulse python3 collector.py --discover` — prints every
   `SYNO.SynologyDrive.*` API the NAS exposes plus a sample log entry.
2. Compare the sample entry's field names to the `F_*` lists; add/reorder.
3. If discovery is ambiguous, cross-check in browser DevTools (Network tab)
   while clicking around Drive Admin Console > Log and > Client List.
4. Run a real collect and sanity-check the dashboard against what Admin
   Console shows for one known-active user.
5. Add `--file-counts` and check runtime on a realistic dataset.

Also unverified because no registry was reachable from the build environment:
the image has never actually been built. `docker buildx` on a Mac is the first
real test of the Dockerfile.

## Prerequisites per NAS

- Service account (e.g. `svc-drivemonitor`) in the administrators group (Drive
  Admin Console data is admin-only), **no 2FA** (no OTP handling), ideally
  source-IP restricted via DSM firewall.
- Container Manager installed (DSM 7).
- Drive Admin Console log retention ≥ the history window, or the calendar is
  truncated.

## Next steps

1. Push to GitHub; Actions builds and publishes the image.
2. Make the GHCR package **public** (once) so client NASes pull without
   credentials. Until then every site would need `docker login ghcr.io`.
3. Deploy to one pilot NAS and run the validation loop above.
4. Once field mappings are confirmed, roll out to remaining sites and pin each
   to a version tag rather than `:latest`.
5. Consider per-client SLA thresholds — currently hardcoded in `status_for()`.

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
