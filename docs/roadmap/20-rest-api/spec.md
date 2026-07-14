# 20 — REST API + API keys

**Phase:** 5 — Integrate · **Effort:** M · **Depends on:** — (15 makes job control exposable) · **Issue:** —

## Goal

A small, token-authenticated REST API so downloads can be triggered and observed
from **outside the browser UI**: phone shortcuts, n8n/Home-Assistant flows, Telegram
bots, cron scripts. The job machinery behind it exists completely — this feature is
an authenticated front door.

## Current state

- All routes are NiceGUI pages behind `AuthMiddleware` (session-cookie OIDC) — no
  programmatic access path exists.
- NiceGUI runs on FastAPI; plain API routes can be added alongside the UI.

## API design (v1, deliberately small)

Base path `/api/v1`, auth via `Authorization: Bearer <key>`, JSON in/out:

| Method & path | Does |
|---|---|
| `POST /api/v1/downloads` | body `{url, mode?, genre?, audio_format?, destination?}` → enqueue via `start_job`, defaults from the key owner's `UserSettings`; returns `{job_id}` (202) |
| `GET /api/v1/downloads/{id}` | live `JobState`/history mirror: phase, counts, error, warning |
| `GET /api/v1/downloads` | recent history (paginated, the key owner's rows) |
| `GET /api/v1/library/search?q=` | index lookup (artist/title match) — "do I have this already?" |
| `GET /api/v1/health` | 200 + version (unauthenticated — for uptime monitors) |

Semantics: `mode` optional → feature 02's `suggest_mode` when merged, else default
mode from settings; `browser` destination is rejected (an API client can't receive
a browser push — WebDAV only, clear 422 message).

## API keys

- Per-user, multiple keys (label per key — "n8n", "phone"), managed on the settings
  page: create (secret shown **once**), revoke, `last_used_at` display.
- Stored **hashed** (SHA-256 — one-way; unlike WebDAV credentials there is no need
  to ever read it back, so no Fernet here). Prefix format `sp_<random>` with the
  first 8 chars stored in plaintext for identification in the UI list.
- Every API request resolves the key → user; all data access is scoped to that
  user exactly like a session would be.

## Scope

**In:** the five endpoints, key management (model + settings card), Bearer auth
dependency, `AuthMiddleware` exemption for `/api/*` (the API does its own auth),
uniform JSON error shape, minimal per-key rate limit (e.g. 60 req/min, in-memory —
protects against a misbehaving script, not against attackers), README section with
`curl` examples.

**Out:**

- Full CRUD for settings/subscriptions via API (grow later by demand).
- OAuth/OIDC client-credentials for the API (Bearer keys suffice for the
  self-hosted audience).
- Webhooks/callbacks on job completion (the notification system already does
  event-push; an API client can poll).
- OpenAPI docs exposure (FastAPI generates it; **disable** `/docs` for the app or
  gate it — decide in implementation, default: keep schema internal).

## Acceptance criteria

1. `POST /downloads` with a valid key + YT URL enqueues a job observable in the UI
   and via `GET /downloads/{id}` until `done`.
2. Missing/invalid/revoked key → 401 with JSON error; key of user A can never see
   or create user B's data (test both directions).
3. Keys are hashed at rest; the plaintext appears exactly once in the create
   response; revocation takes effect immediately.
4. `destination=browser` → 422; invalid URL → 422 with the same validation the UI
   uses (`is_supported_url`).
5. Rate limit answers 429 with `Retry-After`; the health endpoint needs no auth.
6. The browser UI's session auth is completely unaffected (`AuthMiddleware`
   regression tests).
7. i18n not required for API bodies (English, machine-consumed — documented
   decision); settings-page strings translated (de + en); suite green.
