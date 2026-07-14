# 22 — Storage monitor & space guard

**Phase:** 6 — Resilience · **Effort:** S–M · **Depends on:** — · **Issue:** —

## Goal

Make disk space **visible** (WebDAV quota + local staging usage) and make the app
**refuse to start a big run into a full disk** — a full staging volume mid-artist-run
currently means cryptic ffmpeg/copy errors halfway through a 40-album job.

## Current state

- `LOCAL_MUSIC_ROOT` (staging) fills up invisibly; no check anywhere before or
  during a job.
- The WebDAV server's quota (oCIS/OpenCloud/Nextcloud all support RFC 4331 quota
  properties) is never queried.
- Failures from ENOSPC surface as generic job errors after wasted download time.

## Two halves

### 1. Visibility

- **Staging**: `shutil.disk_usage(LOCAL_MUSIC_ROOT)` — free/total, shown in the
  settings WebDAV/system area and (compact) on the download page near the job
  cards.
- **WebDAV quota**: RFC 4331 `quota-available-bytes` / `quota-used-bytes` via
  PROPFIND on the user's `webdav_folder`. Servers that don't report it (or report
  unlimited, `-1`) → "quota unknown/unlimited", displayed as such — never an error.
- Both refreshed on page load + a refresh button; values cached a few minutes
  (module-level TTL) so page rendering never hammers the WebDAV server.

### 2. Guard

- New app-level setting `min_free_staging_mb` (env, default **2048**): when free
  staging space is below it, **new jobs are rejected at enqueue time** with a
  translated error (existing jobs finish — mid-job checks stay out, see below).
- Soft warning band (below 2× the threshold): jobs start, but the download page
  shows a warning chip.
- WebDAV quota guard is **warn-only** (upload failures are already retried +
  surfaced per-file; hard-blocking on a quota number that servers report
  inconsistently would cause false refusals).

## Scope

**In:** the visibility surfaces, the enqueue-time guard + warning band, quota
PROPFIND with graceful degradation, i18n.

**Out:**

- Mid-job space monitoring/pausing (job-level ENOSPC already fails "loudly";
  the guard prevents the common case — starting big runs on a nearly-full disk).
- Auto-cleanup of staging (the pipeline already cleans up per job; leftover
  orphans from crashes are feature 05 territory — add a cheap "stale staging
  dirs" check there when both exist).
- Per-user staging quotas (single shared volume, single threshold).

## Acceptance criteria

1. Settings/system view shows staging free/total; with a quota-reporting WebDAV
   server also used/available; with a non-reporting server "unknown" without
   errors.
2. With free staging space below the threshold, `start_job`/`start_sync`/API
   enqueue refuse with a clear message; above it, everything works as today.
3. The warning band chip appears/disappears correctly around 2× threshold.
4. Quota PROPFIND failures (403, missing props, timeouts) degrade to "unknown" —
   never break the settings page or a job.
5. The scheduler's interval syncs respect the same guard (a full disk stops
   auto-syncs with a logged reason + `last_status=error`, `notify_sync_error`
   fires if enabled — a silent stop would be worse than the full disk).
6. i18n complete (de + en); suite green; no pipeline/tagging changes.
