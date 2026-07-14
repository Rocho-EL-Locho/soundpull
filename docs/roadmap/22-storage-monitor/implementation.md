# 22 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/storage_monitor.py` | **new** — staging usage, quota PROPFIND, guard predicate, TTL cache |
| `app/config.py` | `min_free_staging_mb: int = 2048` (env) |
| `app/jobs.py` | guard call in `start_job` / `start_sync` (and `start_batch`/API if merged) |
| `app/webdav_util.py` | low-level quota PROPFIND helper |
| `app/pages/settings.py` | storage card (staging + quota + refresh) |
| `app/pages/index.py` | warning chip near job cards |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_storage_monitor.py` (new), `tests/test_jobs.py` | see Testing |

## Step plan

### 1. `app/storage_monitor.py`

```python
@dataclass(frozen=True)
class StagingSpace:
    free_bytes: int; total_bytes: int

@dataclass(frozen=True)
class QuotaInfo:
    used_bytes: int | None; available_bytes: int | None   # None = unknown/unlimited

def staging_space() -> StagingSpace          # shutil.disk_usage(settings.local_music_root)
def webdav_quota(user_id: int) -> QuotaInfo  # cached (TTL ~300s) per user
def staging_guard() -> Literal["ok", "warn", "block"]:
    # free < min_free → block; free < 2*min_free → warn; else ok
```

- Cache: module dict `{user_id: (timestamp from time.monotonic(), QuotaInfo)}` with
  a lock; `refresh=True` param bypasses (the refresh button).
- All network/OS errors inside `webdav_quota` → `QuotaInfo(None, None)` + debug log.

### 2. Quota PROPFIND (`app/webdav_util.py`)

- webdav4's `Client` may expose quota props via its `info()`/propfind wrapper —
  **check the installed webdav4 version first**; if `info()` doesn't return
  `quota-available-bytes`, do a raw `PROPFIND` (Depth 0) through the client's
  underlying httpx session (`client.http` — verify attribute name) with an explicit
  props body requesting `DAV: quota-available-bytes` / `quota-used-bytes`, parsed
  with `xml.etree` (namespace-aware, defensive).
- Values: missing prop or `-1`/`-2` (RFC + Nextcloud conventions for
  unlimited/unknown) → `None`. Non-negative ints parsed as bytes.
- Keep it a pure client-level helper (`get_quota(client, path) -> tuple[int|None,
  int|None]`), SSRF/allowlist already handled by `make_client`.

### 3. Guard wiring (`app/jobs.py`)

- At the TOP of `start_job` and `start_sync` (before any state/DB writes):
  `if staging_guard() == "block": raise JobRejected(t-key)` — introduce a typed
  exception; UI call sites catch it and `ui.notify` the translated message; the
  scheduler's sync path catches it, sets `last_status="error"` +
  `last_error=<translated via i18n.translate(cfg.language,…)>` and lets
  `notify_sync_error` fire through the existing error-notification path (read
  `_run_sync`'s except block — the rejection should flow through the same
  reporting, NOT crash the tick loop).
- API (feature 20, if merged): map `JobRejected` → 507 Insufficient Storage.

### 4. UI

- Settings: a "Storage" card — staging progress bar (used/total with the block
  threshold marked), quota line (or "unknown"), refresh button
  (`run.io_bound(webdav_quota, user_id, refresh=True)`).
- Index page: chip near the job area when `staging_guard() != "ok"` (yellow warn /
  red block text), evaluated on render + piggybacked on the existing `ui.timer`
  job refresh (cheap — `disk_usage` is a syscall; quota is NOT polled here, staging
  only).
- Formatting helper for bytes (GiB, one decimal) — check whether one exists (the
  history/index pages may have one); add to a neutral module if not, and reuse.

## Testing

- `staging_guard` thresholds: block/warn/ok boundaries (monkeypatched
  `disk_usage`, configured threshold).
- Quota XML parsing: fixture PROPFIND responses (oCIS-style, Nextcloud-style with
  `-3`, missing props, malformed XML → unknown, never raises).
- Cache: second call within TTL hits cache; `refresh=True` bypasses (monotonic
  clock monkeypatched).
- Guard wiring: `start_job` under "block" raises `JobRejected` before any
  `JobState` is registered (registry unchanged); `start_sync` rejection sets
  subscription error state + triggers the error-notify path (extend
  `tests/test_jobs.py` with spies).
- No live WebDAV in tests (fake client seam as usual).

## Definition of done

Acceptance criteria pass; manual verification: set `min_free_staging_mb` above the
dev machine's real free space → job refused with message; normal value → runs;
quota display checked against the real oCIS/OpenCloud server; suite green; version
bumped; PR.
