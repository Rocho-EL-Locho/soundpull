# 02 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/sources.py` | **new** — `SourceSpec`, registry, `detect_source`, `suggest_mode` |
| `app/pipeline.py` | consume `SourceSpec` for extractor-args/cookies/POT; keep flag lists frozen |
| `app/pages/index.py` | mode auto-suggestion on URL change |
| `app/pages/subscriptions.py` | keep `is_supported_url` call (now source-aware for free) |
| `tests/test_sources.py` (new), `tests/test_pipeline.py` | detection table + parity proof |

## Step plan

### 1. `app/sources.py`

```python
@dataclass(frozen=True)
class SourceSpec:
    key: str                      # "youtube"
    label: str                    # "YouTube Music"
    extractor_args: str | None    # "youtube:player_client=android_vr,mweb"
    supports_cookies: bool        # per-user cookie applies
    supports_pot: bool            # PO-token provider applies
    supports_artist: bool         # artist mode available
    cover_square_crop: bool       # thumbnails may be 16:9 → crop
    matches: Callable[[str], bool]
    suggest_mode: Callable[[str], str | None]

YOUTUBE = SourceSpec(key="youtube", extractor_args=EXTRACTOR_ARGS_YT, ...)
_REGISTRY: tuple[SourceSpec, ...] = (YOUTUBE,)

def detect_source(url: str) -> SourceSpec | None: ...
def is_supported_url(url: str) -> bool:   # re-exported for existing call sites
    return detect_source(url) is not None
```

- Move the host-matching logic of `pipeline.is_supported_url` / `_YOUTUBE_HOSTS`
  here **verbatim**; `pipeline.is_supported_url` becomes an import/re-export so
  `index.py` / `subscriptions.py` need no edits (or update their imports — either way,
  one source of truth).
- The `EXTRACTOR_ARGS` **string constant stays in `pipeline.py`** (it is part of the
  frozen flag lists); `sources.py` references it. No circular import: `sources`
  imports the constant from a small module-level location — if a cycle appears, move
  the constant into `sources.py` and have `pipeline` import it (constant relocation,
  value untouched; snapshot test proves it).

### 2. Pipeline consumption (parity-safe)

- Add `_apply_source(flags: list[str], source: SourceSpec) -> list[str]` next to
  `_apply_audio_format()`:
  - `source.key == "youtube"` → **return the list unchanged** (identity — the parity
    baseline).
  - otherwise → replace/drop the `--extractor-args youtube:…` pair and insert the
    source's own `extractor_args` if set.
- `_extractor_args()` (used by probes): take the source's args instead of the
  constant; identical value for YouTube.
- `_apply_cookie_policy` / `_apply_pot_provider`: call sites gate on
  `source.supports_cookies` / `source.supports_pot`. For YouTube both are `True` →
  behavior identical.
- `run_download` / probes / `enumerate_*`: detect the source once at the top from the
  URL and thread the `SourceSpec` through (parameter with `default=None` →
  `detect_source(url)` inside, so external callers don't all change at once).
- `enumerate_artist`: guard with `source.supports_artist`; keep the YT implementation
  as-is. Feature 06 adds the dispatch.

### 3. Mode suggestion + UI

- `suggest_mode` for YouTube per the table in `spec.md` (parse with
  `urllib.parse.urlparse`/`parse_qs`; `OLAK5uy_` prefix check for album lists).
- `app/pages/index.py`: on the URL input's `on_value_change`, call
  `detect_source(url)`, and if it yields a suggestion **and** the user hasn't
  manually changed the mode since the last URL edit, set the mode toggle. Simplest
  robust rule: keep a `manual_mode_override` flag that is set by the toggle's own
  change handler and cleared whenever the URL value changes.
- The dedup-auto-on-for-artist behavior (existing, ~`index.py:194`) must keep firing
  when the mode is set programmatically — verify the toggle's change handler runs
  (in NiceGUI, setting `.value` triggers the handler; if not, call it explicitly).

### 4. Tests

- `tests/test_sources.py`:
  - detection table: all `_YOUTUBE_HOSTS` variants, `music.youtube.com`, unknown
    hosts → `None`, garbage strings → `None` (no exception).
  - `suggest_mode` table from `spec.md`.
  - dummy-source registration test (monkeypatch the registry): detection +
    `_apply_source` produces a list without any `youtube:` extractor-args.
- `tests/test_pipeline.py`:
  - **parity proof**: `_apply_source(_ALBUM_FLAGS, YOUTUBE) == _ALBUM_FLAGS` (and
    same for `_SINGLE_FLAGS`); existing options snapshot untouched.

## Definition of done

Acceptance criteria in `spec.md` pass; snapshot/parity tests green; a real YouTube
album download re-verified once (format + tags unchanged); version bumped; PR opened.
