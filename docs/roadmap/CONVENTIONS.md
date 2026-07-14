# Conventions & invariants (read before implementing ANY feature)

These rules apply to every feature in this roadmap. Violating the first one is a
release-blocker.

## 1. ⚠️ Metadata parity — the one rule that must not break

Tag output of the default configuration must stay **byte-identical** to the original
bash tool. Two mechanisms guarantee this:

1. yt-dlp is configured by feeding the **exact original CLI flag lists**
   (`_ALBUM_FLAGS` / `_SINGLE_FLAGS` in `app/pipeline.py`) into
   `yt_dlp.parse_options()`. Never edit these lists in place for a feature; follow the
   `_apply_audio_format()` precedent instead — a *transform* over the list whose
   default case is a **no-op**.
2. Tag normalization lives in `app/fix_music_tags.py` and its rules are **frozen**.
   Do not refactor them. Parity-safe *extensions* exist as precedent (e.g. the
   `album_artist` fallback that only fires when no explicit value is passed) — any
   extension must leave the default path bit-identical and be guarded by tests.

After ANY change near the pipeline or tagging: run `tests/test_pipeline.py` and
`tests/test_fix_music_tags.py`, and re-verify with a real download when the change
touches yt-dlp options. Quick no-download check:

```bash
.venv/bin/python -c "from app.pipeline import _ALBUM_FLAGS,_build_ydl_opts;import pprint;pprint.pp(_build_ydl_opts(_ALBUM_FLAGS+['-o','/tmp/x/%(title)s.%(ext)s']).get('postprocessors'))"
```

Known deliberate parity **deviation** (0.8.8): `feat.` markers inside the ARTIST tag are
normalised to ` / `. Do not "restore parity" by reverting it.

## 2. Database

- Schema migration is **additive-only and automatic**: `init_db()` in `app/db.py` runs
  `create_all()` + `reconcile_columns()`, which `ALTER TABLE … ADD COLUMN`s missing
  model columns and backfills scalar defaults. **Adding a column or table is safe**;
  drops/renames/type-changes are NOT handled.
- For a NOT NULL column the default must be a **scalar** (`Field(default=…)`, not
  `default_factory`); datetime columns fall back to `CURRENT_TIMESTAMP`.
- `app/models.py` must **NOT** use `from __future__ import annotations` (breaks
  SQLModel relationship forward-refs). Other modules may.

## 3. i18n

- Every user-facing string goes through `t("key")` from `app/i18n.py`. Add each new key
  to **both** `de` and `en` (a test enforces key parity).
- `t()` needs a request context (`app.storage.user`) — use it only in render/handler
  code; module-level constants stay language-neutral. Background workers resolve
  strings explicitly via `i18n.translate(language, key)`.

## 4. UI / pages

- NiceGUI + Quasar + Tailwind, dark "glass" theme. Style with `.classes()` (Tailwind)
  and `.props()` (Quasar). For toggles use `toggle-color` (not `color`).
- Routing is a **single app-shell** with a client-side `ui.sub_pages` router in
  `app/main.py`. A new page is a `*_content()` builder function in `app/pages/`,
  registered in `main.py`'s router and linked in the nav of `frame()`
  (`app/theme.py`) — NOT a standalone `@ui.page`.
- Long-running work never blocks the event loop: use the jobs worker
  (`app/jobs.py`) or `run.io_bound`, and surface progress via `ui.timer` polling.

## 5. Jobs & side effects

- Blocking downloads run in the bounded `ThreadPoolExecutor` in `app/jobs.py`; the UI
  reads in-memory `JobState`; the `DownloadHistory` row is the durable record.
- Side effects around a job are **best-effort and isolated** — follow the
  `_record_delivered_safe` / `_notify_safe` pattern (log + swallow, never fail the job).

## 6. Secrets & outbound URLs

- Secrets (WebDAV/SMTP passwords, tokens, cookies) are Fernet-encrypted at rest
  (`app/security.py`) and exposed to the client **only** as `has_*` flags — never
  plaintext.
- Any new user-configurable outbound URL needs the same SSRF guard pattern as
  WebDAV/notifications: http(s)-only + optional host allowlist
  (see `_check_host_allowed` in `app/webdav_util.py`, `_valid_http_url` in
  `app/notifications.py`).

## 7. WebDAV & paths

- Remote paths go through `_SafePathClient` (percent-encodes `%`/`#`/`?`) and
  `_safe_segment` (maps `?*:"<>|` to fullwidth look-alikes — oCIS/OpenCloud rejects
  raw `?` even percent-encoded).
- `LOCAL_MUSIC_ROOT` is **temp/staging only**, never a final destination.
- All remote operations must stay confined to the user's `webdav_folder` base.

## 8. Dev & test

- Dev login works without OIDC vars (`/login` auto-creates a dev user).
- The server runs with `reload=False` — the **user restarts it manually**; never
  start/stop it yourself, just say a restart is needed.
- ffmpeg must be on PATH.
- Tests: `pytest` in `tests/`, logic-level (no live network, no NiceGUI render).
  Run with `.venv/bin/python -m pytest`. New logic gets tests in the same style.
- yt-dlp is **pinned exactly** in `pyproject.toml`; don't bump it as a side effect.

## 9. Git / PR

- `main` is protected — every feature goes through a branch + PR (repo PR template).
- Commits use the repo owner's identity; **no AI co-author trailers**.
- Bump the version in `pyproject.toml` in the PR (see the `feat: … (bump x.y.z)`
  convention in `git log`): feature PR → next free **minor**, fix PR → patch.
  Milestone/version targets live in the roadmap README ("Versioning & releases").
- Reference the linked GitHub issue in the PR body when the spec names one.
