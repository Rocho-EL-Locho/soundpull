"""Application entry point: wire NiceGUI, auth, pages and run the server."""
from __future__ import annotations

import logging

from nicegui import app, ui

from app import auth
from app.config import settings
from app.db import init_db

# Import page modules so their @ui.page routes are registered.
from app.pages import history, index, settings as settings_page  # noqa: F401,E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

# Initialize DB and register auth routes before serving.
init_db()
auth.init_auth()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# Gate every page behind authentication (must be added before the server starts).
app.add_middleware(auth.AuthMiddleware)

if not settings.oidc_configured:
    log.warning("OIDC is not configured — /login uses a local DEV user. Set OIDC_* in .env for authentik.")


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="Soundpull",
        favicon="📥",
        storage_secret=settings.session_secret,
        reload=False,
        show=False,
    )
