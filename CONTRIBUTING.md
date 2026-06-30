# Contributing

Thanks for your interest in Soundpull!

## Dev setup

```bash
python -m venv .venv && .venv/bin/pip install ".[test]"
.venv/bin/python -m app.main     # http://localhost:8080 (dev login, no authentik needed)
```

Requires `ffmpeg` on PATH. With the `OIDC_*` env vars unset, `/login` uses a local
dev user so you can run the UI without authentik.

## Tests

```bash
.venv/bin/python -m pytest -q
```

CI runs the same on every push/PR.

## Guidelines

- Keep changes focused; match the surrounding style.
- **Do not change the metadata behaviour casually.** Tag output must stay identical to
  the original tool — the yt-dlp flag lists in `app/pipeline.py` and the rules in
  `app/fix_music_tags.py` are covered by tests in `tests/`. If you touch them, update and
  run the tests and verify real output. See `CLAUDE.md` for the full rationale.
- Run the test suite before opening a PR.

## Reporting bugs / ideas

Use the issue templates. For security issues, see [SECURITY.md](SECURITY.md).
