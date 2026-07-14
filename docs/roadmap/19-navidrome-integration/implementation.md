# 19 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **Model everything
on `app/notifications.py`** — same settings shape, same best-effort trigger, same
test button; this feature is deliberately a structural copy of that proven pattern.

## Touch points

| File | Change |
|---|---|
| `app/navidrome.py` | **new** — Subsonic auth, `ping`, `start_scan`, config snapshot |
| `app/models.py` | `UserSettings`: `navidrome_url`, `navidrome_username`, `navidrome_password_enc`, `navidrome_scan_after_upload` (+ `has_navidrome_password` property) |
| `app/jobs.py` | `_navidrome_safe(user_id)` trigger after successful WebDAV delivery (3 call sites) |
| `app/pages/settings.py` | Navidrome card (fields + toggle + test button) |
| `app/config.py` | optional `navidrome_host_allowlist` (mirror `webdav_host_allowlist`) |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_navidrome.py` (new), `tests/test_jobs.py` | see Testing |

## Step plan

### 1. `app/navidrome.py`

```python
@dataclass(frozen=True)
class NavidromeConfig:
    url: str; username: str; password: str    # decrypted, server-side only

def load_config(user_id: int) -> NavidromeConfig | None   # None when unset/disabled
def _auth_params(cfg) -> dict:
    # salt = secrets.token_hex(8); t = md5((password + salt).encode()).hexdigest()
    # {u, t, s, v: "1.16.1", c: "soundpull", f: "json"}
def ping(cfg) -> None                # GET {url}/rest/ping        → raise NavidromeError on failure
def start_scan(cfg) -> None          # GET {url}/rest/startScan
```

- HTTP via the same client library the notifications module uses (read it — reuse
  its timeout constants and error-wrapping style).
- Response check: Subsonic wraps errors in HTTP 200 — parse the JSON envelope
  (`subsonic-response.status == "ok"`) and surface `error.message` in
  `NavidromeError`.
- URL validation on save AND on use: http(s) + `navidrome_host_allowlist` — reuse
  the exact `_valid_http_url` approach from notifications (import/factor it into a
  shared helper rather than copying a third time — `app/security.py` or a small
  `app/urlguard.py` is the natural home; keep the refactor mechanical).

### 2. Model fields (`app/models.py`)

Follow the WebDAV credential trio pattern verbatim (`webdav_url`/`username`/
`password_enc` + encryption via `app/security.py` in the settings save path);
additive columns → safe migration. `navidrome_scan_after_upload: bool =
Field(default=False)`.

### 3. Trigger (`app/jobs.py`)

`_navidrome_safe(user_id)`: mirror `_notify_safe` exactly — fresh config load,
decrypt, one call, try/except log-and-swallow. Call sites (all only when
destination was WebDAV and delivery succeeded):

- `_run` success path (after `_persist` of `done`),
- `_run_artist` success path (ONCE, after the aggregate result),
- `_run_sync` success path **guarded by `new_track_count > 0`**.

Read each function's terminal block and place the call next to the existing
`_notify_safe` invocation — same ordering rationale (job state first, side effects
after).

### 4. Settings UI

Card between WebDAV and Notifications: url/username/password inputs (password
`props('type=password')`, placeholder shows "saved" state via `has_*` like WebDAV),
scan toggle, test button → `run.io_bound(ping, …)` on **saved** settings with
success/error notify (copy the notification-test button's handler shape,
`settings.py` ~L188).

### 5. Deep links

Expose `navidrome_link(base_url, artist=…, album=…) -> str` in `app/navidrome.py`
(pure string builder to the web UI's search route) so features 03/04/05 import it
from one place. If 03 already merged with its own local version, consolidate here.

## Testing (`tests/test_navidrome.py`)

- `_auth_params`: token = md5(password+salt), salt random per call, version/client
  constants present.
- `ping`/`start_scan` against a monkeypatched HTTP layer: ok envelope, Subsonic
  error envelope → `NavidromeError` with message, network error → `NavidromeError`.
- URL guard: non-http, allowlist miss → rejected before any request.
- Trigger wiring (extend `tests/test_jobs.py`): success → called once; failure →
  job still `done` (spy raising inside `_navidrome_safe`'s call); sync with 0 new
  tracks → not called.
- Secret handling: config never serialized to the client (settings payload check,
  same style as the notifications no-secret tests).

## Definition of done

Acceptance criteria pass; manual verification against a real Navidrome instance
(scan fires, new album appears immediately; wrong password → clean test-button
error); suite green; version bumped; PR.
