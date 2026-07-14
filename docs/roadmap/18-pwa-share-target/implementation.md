# 18 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/static/manifest.webmanifest`, `app/static/sw.js`, `app/static/icons/*` | **new** static assets |
| `app/main.py` | serve statics; SW route at origin scope |
| `app/theme.py` | `<link rel="manifest">` + meta/theme-color + SW registration snippet in `frame()` |
| `app/auth.py` | preserve query string through the login redirect |
| `app/pages/index.py` | prefill from `url`/`text` query params |
| `app/pages/settings.py` | install-hint blurb (i18n) |
| `tests/test_auth.py`, `tests/test_share_prefill.py` (new) | see Testing |

## Step plan

### 1. Static assets

- `manifest.webmanifest`: name "Soundpull", short_name, `start_url: "/"`,
  `display: "standalone"`, `background_color`/`theme_color` from the glass theme's
  base (read the actual values in `app/theme.py`), icons array (192, 512,
  512-maskable), `share_target` as in spec.
- Icons: generate PNGs once (script or manual) — simple mark (e.g. "S" glyph on the
  theme gradient); commit the PNGs, don't generate at runtime.
- `sw.js` (deliberately minimal):

  ```js
  self.addEventListener('install', () => self.skipWaiting());
  self.addEventListener('activate', (e) => e.waitUntil(clients.claim()));
  self.addEventListener('fetch', () => {});   // network passthrough, no caching
  ```

  An empty fetch handler satisfies installability without ever intercepting —
  re-verify installability with Lighthouse since requirements shift; if a
  non-empty handler is required, `fetch(event.request)` passthrough only.

### 2. Serving (`app/main.py`)

- NiceGUI/FastAPI static mount: `app.add_static_files('/static', <app/static dir>)`
  (NiceGUI API — verify name in the installed version).
- The SW **must** be served from `/sw.js` (origin scope, not `/static/sw.js` — a SW
  can only control its path scope): add a tiny FastAPI route returning the file
  with `Service-Worker-Allowed` not needed if served at root; content-type
  `text/javascript`, and `Cache-Control: no-cache` so updates propagate.

### 3. Shell wiring (`app/theme.py`)

In `frame()` (or better: once in the page setup used by all pages —
`ui.add_head_html`): manifest link, `theme-color` meta, apple-touch-icon, and a
registration snippet:

```html
<script>
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
</script>
```

### 4. Prefill (`app/pages/index.py`)

- Read query params in `index_content` (NiceGUI: `ui.context.client.request` or the
  sub-pages router's query handling — check how existing code accesses request
  context, `app.storage.user` usage shows the pattern).
- Extraction helper (pure): `shared_url(params: dict) -> str | None` — prefer
  `url`, else first `https?://\S+` regex hit in `text`, else `title`; validate with
  the existing URL gate (`is_supported_url`) before prefilling; unsupported →
  prefill anyway but let the existing validation message show (no new logic).
- After prefill, strip the params from the visible URL
  (`ui.run_javascript('history.replaceState(...)')`) so a reload doesn't re-fill.

### 5. Login redirect (`app/auth.py`)

Read the current flow: where the intended target is stored when `AuthMiddleware`
bounces to `/login` (session key or `next` param). Extend it to store
`request.url.path + "?" + request.url.query` (query included) and use it in the
callback's final redirect. **Regression care:** this is the auth path — keep the
change minimal and covered by `tests/test_auth.py` (existing redirect test gets a
query-string case).

## Testing

- `shared_url` table: `url` param, URL inside `text` prose, `text` without URL,
  precedence, garbage.
- Auth redirect preserves query (extend `tests/test_auth.py` — logic-level, follows
  the existing test style).
- Manifest JSON validity + share_target shape (load + assert keys — cheap guard
  against typos).
- **Manual (mandatory):** Lighthouse installability; real Android install; share
  from YT Music app (logged in AND logged out); desktop regression (no SW cache
  weirdness after a redeploy).

## Definition of done

Acceptance criteria pass incl. the manual matrix; suite green; version bumped; PR.
