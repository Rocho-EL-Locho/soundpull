# 21 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/cookie_monitor.py` | **new** — probe, judgement, state transition, notify |
| `app/models.py` | `UserSettings`: `cookie_status: str = Field(default="unknown")`, `cookie_checked_at: datetime | None`, `cookie_fail_count: int = Field(default=0)`, `notify_cookie_invalid: bool = Field(default=True)` |
| `app/scheduler.py` | daily due-check per user-with-cookie |
| `app/config.py` | `cookie_probe_video_id: str` (env, default a stable age-restricted id chosen at implementation time) |
| `app/pipeline.py` | none — reuse `_apply_cookie_policy` / `_apply_socket_timeout` / `_apply_pot_provider` (import them; if private-name imports feel wrong, expose a `build_probe_opts()` helper there) |
| `app/notifications.py` | new event kind (title/message keys), payload = status only |
| `app/pages/settings.py` | status chip + save-time probe; toggle in notifications card |
| `app/pages/index.py` | invalid-cookie banner |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_cookie_monitor.py` (new) | see Testing |

## Step plan

### 1. Probe (`app/cookie_monitor.py`)

```python
class ProbeResult(Enum): OK, INVALID, UNKNOWN

def probe_cookie(user_id: int) -> ProbeResult:
    # build minimal ydl opts: quiet, skip_download=True,
    # then the SAME _apply_cookie_policy / _apply_socket_timeout /
    # _apply_pot_provider chain a real run gets (this is the point — test the
    # real path). extract_info(watch_url(cookie_probe_video_id))
```

- Judgement mapping: successful extraction with actual formats → `OK`.
  `DownloadError` whose message matches the age-gate/login patterns ("Sign in to
  confirm your age", "confirm you're not a bot", "cookies" hints — collect the
  exact strings from yt-dlp's youtube extractor at the pinned version and keep
  them in one tuple with a comment) → `INVALID`. Everything else → `UNKNOWN`.
- `run_check(user_id)`: probe → state transition table:

  | old status | result | new status | side effect |
  |---|---|---|---|
  | any | UNKNOWN | unchanged | stamp checked_at only |
  | ok/unknown | INVALID | fail_count += 1; `invalid` only when ≥ 2 | notify on the flip |
  | invalid | OK | `ok`, fail_count 0 | recovery notice |
  | ok | OK | stamp only | — |

- Notify through the existing channel senders with an explicit-language resolve
  (`i18n.translate(cfg.language, …)` — copy the `NotifyConfig` usage from
  `jobs._notify_safe`); payload fields: status + checked-at, nothing else.

### 2. Scheduler (`app/scheduler.py`)

In `_tick`: users where `youtube_cookies_enc` is set and `cookie_checked_at` older
than 24 h → `run_check` (best-effort per user, log + continue). Reuse the interval
pattern from feature 03's library-scan due-check if merged; keep both checks
structurally identical.

### 3. UI

- Settings, cookie card: status chip (`ok` green / `invalid` red / `unknown`
  neutral, + relative time); saving a cookie triggers `run.io_bound(run_check…)`
  and refreshes the chip (save FIRST, probe the saved value — consistent with the
  test-button convention).
- Index page: thin warning banner at top when the current user's status is
  `invalid` (`ui.banner`/styled row, dismiss = session-local via
  `app.storage.user["cookie_banner_dismissed"]`, reappears next session while
  still invalid).

### 4. Notifications toggle

`notify_cookie_invalid` in the notifications card next to the other three event
toggles; respected by `run_check`'s notify step. Default True is fine because
without configured channels nothing sends anyway (channel-config check is the
existing outer guard — verify that guard's location in `notifications.py` and rely
on it).

## Testing (`tests/test_cookie_monitor.py`)

- Judgement mapping with monkeypatched `extract_info`: success, each age-gate
  message → INVALID, timeout/network → UNKNOWN.
- Transition table: all rows above, incl. exactly-one-notification on flip
  (spy), none on repeat, recovery notice once.
- Debounce: single INVALID does not flip status.
- Scheduler due-logic: no cookie → never probed; fresh check → skipped.
- No-secret: notification payload contains no cookie material (payload dict
  assertion, style of the existing notification tests).

## Definition of done

Acceptance criteria pass; manual verification: real valid cookie → chip OK; corrupt
the stored cookie deliberately → after two forced checks the banner + one ntfy
message appear; suite green; version bumped; PR.
