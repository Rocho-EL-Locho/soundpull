# 17 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/exports.py` | **new** — pure serializers (rows → CSV/JSON strings) |
| `app/pages/settings.py` | "Export & backup" card: 3 export buttons + settings import |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_exports.py` (new) | see Testing |

## Step plan

### 1. `app/exports.py` — pure, UI-free

```python
def library_manifest_csv(user_id: int) -> str
def library_manifest_json(user_id: int) -> str
def history_csv(user_id: int) -> str
def settings_json(user_id: int) -> str
def apply_settings_json(user_id: int, payload: str) -> ImportResult   # applied, skipped, errors
```

- Library rows: `ServerTrack` query + `split_rel_path` for display artist/album
  (helper from feature 03 if merged; else a local minimal version with a note to
  consolidate). CSV header starts `artist,title` (the feature-12 contract),
  followed by `album,rel_path`.
- CSV via stdlib `csv` into `io.StringIO` (correct quoting for commas/quotes in
  titles). Decide BOM: **yes, UTF-8 BOM** for the CSVs (Excel-friendly umlauts) —
  document in the module docstring; JSON without BOM.
- **Secret exclusion** in `settings_json`: explicit ALLOWLIST of exportable fields
  (defaults, tag toggles, language, webdav url/folder/username — NOT password —,
  dedup/lyrics toggles, notification toggles + non-secret URLs, intervals). An
  allowlist cannot leak future fields by accident; the test additionally asserts a
  DENY-pattern (`_enc`, `password`, `token`, `cookie`) against whatever is in the
  output as a second net.
- `apply_settings_json`: parse, iterate allowlist only, type-check each value
  against the model field (reject wrong types into `skipped`), write via the same
  session pattern the settings page's `save()` uses. Unknown keys → `skipped`.

### 2. Settings UI

- New card "Export & backup" below the WebDAV card:
  - three buttons → handlers build the string via `run.io_bound`, then
    `ui.download.content(data.encode(), filename)` (check the exact NiceGUI
    download-from-memory API in the installed version — `ui.download` accepts
    bytes+filename or a file path; the ZIP delivery in jobs/index shows the
    pattern for files, memory variant preferred here to avoid temp files).
    Filenames: `soundpull-library-<date>.csv` etc. — date from `datetime.now()`
    at click time.
  - settings import: `ui.upload` (single JSON, size-capped) → parse → confirm
    dialog listing the fields about to change → `apply_settings_json` → notify
    result + page reload (settings widgets re-render from DB).
- All via `t()` keys.

### 3. i18n

`settings.export_title`, per-button labels, `settings.import_confirm`,
`settings.import_done` (with applied/skipped counts), `settings.import_secrets_note`.

## Testing (`tests/test_exports.py`)

- Seeded DB → CSV/JSON exact-content assertions (incl. a title containing
  `", "` and umlauts; BOM present).
- Header contract: first CSV columns are `artist,title`.
- `settings_json`: allowlist output only; deny-pattern scan finds nothing; secrets
  set on the user never appear.
- `apply_settings_json`: applies valid, skips unknown + wrong-typed, never writes a
  secret field even if the payload contains one (hostile-import case).
- Empty user → valid empty CSV (header only) / `[]` / minimal JSON.

## Definition of done

Acceptance criteria pass; manual check: export all three on the dev server, open the
CSVs in LibreOffice, re-import the settings JSON; suite green; version bumped; PR.
