# 20 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/api.py` | **new** — FastAPI router, auth dependency, endpoints, rate limit |
| `app/models.py` | **new table** `ApiKey` |
| `app/main.py` | mount router; `AuthMiddleware` exemption for `/api/` |
| `app/pages/settings.py` | "API keys" card (create/list/revoke) |
| `app/i18n.py` | settings-card keys (de + en) |
| `README.md` | API usage section with curl examples |
| `tests/test_api.py` (new) | see Testing |

## Step plan

### 1. Model (`app/models.py`)

```python
class ApiKey(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    label: str
    key_hash: str = Field(unique=True, index=True)   # sha256 hex of full key
    key_prefix: str                                   # "sp_ab12cd34" for display
    created_at: datetime
    last_used_at: datetime | None = None
    revoked: bool = Field(default=False)
```

Additive table → safe. Key generation: `"sp_" + secrets.token_urlsafe(32)`.

### 2. Router (`app/api.py`)

- `router = APIRouter(prefix="/api/v1")`; mount in `main.py` on the NiceGUI
  FastAPI instance (`from nicegui import app` — `app` IS the FastAPI app;
  `app.include_router(router)`).
- Auth dependency:

  ```python
  def api_user(authorization: str = Header(...)) -> User:
      # parse "Bearer sp_…", sha256, lookup non-revoked ApiKey,
      # stamp last_used_at (throttled: only if > 1 min old — avoid a write per request),
      # return the owning User; else HTTPException 401
  ```

- **Middleware exemption** (`app/main.py`): `AuthMiddleware` currently redirects
  everything unauthenticated to `/login` — read its dispatch and add a pass-through
  for paths starting `/api/` (the router's own dependency handles auth; an
  unauthenticated API call must get 401 JSON, never a 302 to the login page).
  Extend `tests/test_auth.py` for exactly this.
- Endpoints call existing machinery only:
  - `POST /downloads`: validate via `is_supported_url`; destination from body or
    the user's `UserSettings` (reject `browser` with 422); genre/format defaults
    from settings; `jobs.start_job(...)` → `{"job_id": id}`. Read `start_job`'s
    exact signature FIRST — the UI's call in `index.py` (~L225) is the reference
    for required arguments (tag options snapshot etc.).
  - `GET /downloads/{id}`: prefer live `JobState` from the in-memory registry,
    fall back to the `DownloadHistory` row (terminal jobs after restart); 404 for
    other users' ids (ownership check, not just existence).
  - `GET /downloads`: `build_history_query` from `app/pages/history.py` — if
    importable without UI side effects, reuse; else lift the query builder into a
    neutral module (it is logic-only per the tests — check `tests/test_history.py`).
  - `GET /library/search`: `library_index` LIKE query (share the helper feature 03
    adds; else minimal local query).
- Error shape: `{"error": {"code": str, "message": str}}` via exception handlers
  on the router.
- Rate limit: in-memory `{key_id: deque[timestamp]}` sliding window (60/min),
  429 + `Retry-After`. Module-level, lock-guarded, trimmed on access — no
  dependency.
- Pydantic request/response models (FastAPI native) — gives free validation; keep
  them in `api.py`, NOT in `app/models.py` (SQLModel file, future-annotations
  constraint — don't mix).

### 3. Settings card

- List keys (`label`, `key_prefix…`, created, last used, revoke button with
  confirm), create dialog (label input → shows the full key ONCE in a copyable
  `ui.input readonly` + warning it won't be shown again).
- Handlers: plain session-auth page code like the rest of settings; hashing on
  create; revoke sets the flag (keep the row for audit/display).

### 4. Docs exposure decision

NiceGUI/FastAPI serves `/docs` + `/openapi.json` by default — set them off
(`docs_url=None`) **unless** already disabled (check how `ui.run` configures the
app; if the flags aren't reachable through NiceGUI, exclude the API routes from the
schema with `include_in_schema=False` and note it). API examples live in the README
instead.

## Testing (`tests/test_api.py` — FastAPI `TestClient`)

- Auth: no header / bad scheme / unknown key / revoked key → 401 JSON;
  valid key → 200 and `last_used_at` stamped.
- Ownership: user B's key on user A's job id → 404; history list only own rows.
- `POST /downloads`: monkeypatched `start_job` receives correct defaults from
  settings; `browser` destination → 422; invalid URL → 422.
- Rate limit: 61st call in a window → 429 with `Retry-After`.
- `AuthMiddleware`: `/api/...` unauthenticated → 401 not 302; normal pages still
  302 (extend `tests/test_auth.py`).
- Key hashing: plaintext never stored (row inspection), prefix matches.

## Definition of done

Acceptance criteria pass; manual verification: create a key in the UI, trigger a
real download via `curl`, watch it in the browser UI, revoke, confirm 401; suite
green; version bumped; PR.
