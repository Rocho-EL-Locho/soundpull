"""Application entry point: wire NiceGUI, auth, pages and run the server."""
from __future__ import annotations

import logging
from pathlib import Path

from nicegui import app, ui

from app import auth
from app.config import DEFAULT_SESSION_SECRET, settings
from app.db import init_db
from app.pipeline import purge_work_root
from app.scheduler import start_scheduler, stop_scheduler

# Import page modules so their @ui.page routes are registered.
from app.pages import (  # noqa: F401,E402
    history,
    index,
    settings as settings_page,
    subscriptions,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

# Branding assets live inside the package so they ship in the Docker image
# (which only copies `app/`). Served at /static; the favicon is exposed by
# NiceGUI at /favicon.ico (already allow-listed in AuthMiddleware).
STATIC_DIR = Path(__file__).resolve().parent / "static"
FAVICON = STATIC_DIR / "soundpull-favicon.svg"


def _check_production_config() -> None:
    """Fail fast on insecure/incomplete config in a non-local deployment.

    Locally (app_base_url → localhost) the OIDC-less dev login is allowed and the
    shipped session secret is tolerated; in any other deployment both are refused
    so the app never silently serves with no auth or a known cookie secret.
    """
    if settings.is_local_deployment:
        if not settings.oidc_configured:
            log.warning("OIDC not configured — /login uses a local DEV user (local deployment only).")
        return
    if settings.session_secret == DEFAULT_SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is unset/default in a non-local deployment. "
                           "Generate one: openssl rand -hex 32")
    if not settings.oidc_configured:
        raise RuntimeError("OIDC is not configured for a non-local deployment (app_base_url is not "
                           "localhost). Set OIDC_* in .env, or run locally for the dev login.")


_check_production_config()

# Initialize DB, clear stale staging files, and register auth routes before serving.
init_db()
purge_work_root()
auth.init_auth()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# Serve branding assets (logo, banner) for the UI and README.
app.add_static_files("/static", str(STATIC_DIR))

# Gate every page behind authentication (must be added before the server starts).
app.add_middleware(auth.AuthMiddleware)

# Background scheduler for playlist interval-sync (issue #21). Runs in-process; the
# start/stop are no-ops when SYNC_ENABLED=false.
app.on_startup(start_scheduler)
app.on_shutdown(stop_scheduler)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="Soundpull",
        favicon=str(FAVICON),
        storage_secret=settings.session_secret,
        reload=False,
        show=False,
    )
