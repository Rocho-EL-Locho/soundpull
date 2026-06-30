"""OIDC authentication against authentik (Authlib) + NiceGUI session gating.

NiceGUI adds Starlette's SessionMiddleware when `storage_secret` is passed to
`ui.run()`, so `request.session` (used by Authlib for state/nonce) and
`app.storage.user` (server-side, keyed by the session) are both available in
routes, middleware and pages.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse

from authlib.integrations.starlette_client import OAuth, OAuthError
from nicegui import app
from sqlmodel import Session, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import settings
from app.db import session_scope
from app.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from app.models import User, UserSettings

# Routes reachable without authentication.
UNRESTRICTED_ROUTES = {"/login", "/auth/callback", "/logout", "/favicon.ico", "/healthz"}

oauth = OAuth()
if settings.oidc_configured:
    oauth.register(
        name="authentik",
        server_metadata_url=settings.oidc_discovery_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": settings.oidc_scopes},
    )


# ─── User persistence ────────────────────────────────────────────────────────

def upsert_user(session: Session, userinfo: dict) -> User:
    """Create or update the user identified by the OIDC `sub` claim."""
    sub = userinfo["sub"]
    now = datetime.now(timezone.utc)
    user = session.exec(select(User).where(User.sub == sub)).first()
    if user is None:
        user = User(
            sub=sub,
            email=userinfo.get("email"),
            username=userinfo.get("preferred_username"),
            display_name=userinfo.get("name") or userinfo.get("preferred_username"),
            created_at=now,
            last_login_at=now,
        )
        session.add(user)
        session.flush()  # assign user.id
        session.add(UserSettings(user_id=user.id))
    else:
        user.email = userinfo.get("email") or user.email
        user.display_name = userinfo.get("name") or user.display_name
        user.username = userinfo.get("preferred_username") or user.username
        user.last_login_at = now
        session.add(user)
    session.flush()
    return user


def get_current_user(session: Session) -> User | None:
    """Load the DB user for the active session, or None if not logged in."""
    user_id = app.storage.user.get("user_id")
    if not user_id:
        return None
    return session.get(User, user_id)


def current_display_name() -> str:
    return app.storage.user.get("name") or app.storage.user.get("email") or "Account"


# ─── UI language (per-user, durable in UserSettings.language) ─────────────────

def load_user_language(session: Session) -> str:
    """The logged-in user's stored UI language, or the default."""
    user = get_current_user(session)
    lang = user.settings.language if (user and user.settings) else DEFAULT_LANGUAGE
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def set_user_language(lang: str) -> None:
    """Persist the chosen UI language for the current user (DB + session).

    The session mirror is set only after the DB write commits, so a failed
    persist can't leave the session and DB showing different languages.
    """
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            return
        row = session.exec(select(UserSettings).where(UserSettings.user_id == user.id)).first()
        if row is None:
            row = UserSettings(user_id=user.id)
            session.add(row)
        row.language = lang
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
    app.storage.user["lang"] = lang


def _safe_redirect_target(raw: str | None) -> str:
    """Only allow local relative paths (open-redirect guard).

    Rejects protocol-relative (`//host`) and backslash variants (`/\\host`, which
    several browsers normalise to `//host`), plus anything carrying a scheme/host
    or control characters.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if any(c in raw for c in "\\\t\n\r"):
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/"
    return raw


# ─── Route handlers (registered on the NiceGUI FastAPI app) ──────────────────

def init_auth() -> None:
    """Register /login, /auth/callback and /logout on the NiceGUI app."""

    @app.get("/login")
    async def login(request: Request):
        app.storage.user["redirect_to"] = _safe_redirect_target(
            request.query_params.get("redirect_to")
        )

        # Dev fallback: no OIDC configured → log in a local dev user so the UI
        # can be exercised without an authentik instance. Only ever on a local
        # deployment — otherwise this would be an open door (see config.py).
        if not settings.oidc_configured:
            if not settings.dev_login_allowed:
                return Response(
                    "Server-Fehlkonfiguration: OIDC ist nicht eingerichtet.",
                    status_code=503,
                )
            with session_scope() as session:
                user = upsert_user(session, {"sub": "dev-user", "name": "Dev User",
                                             "email": "dev@example.org",
                                             "preferred_username": "dev"})
                _establish_session(user, [])
            return RedirectResponse(app.storage.user.pop("redirect_to", "/"))

        return await oauth.authentik.authorize_redirect(request, settings.oidc_redirect_uri)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.authentik.authorize_access_token(request)
        except OAuthError as exc:
            return Response(f"Login fehlgeschlagen: {exc.error}", status_code=400)

        userinfo = token.get("userinfo") or {}
        if not userinfo.get("sub"):
            return Response("Login fehlgeschlagen: keine Nutzerinfo erhalten.", status_code=400)

        groups = userinfo.get("groups", []) or []
        if settings.oidc_allowed_group and settings.oidc_allowed_group not in groups:
            return Response("Kein Zugriff: erforderliche Gruppe fehlt.", status_code=403)

        with session_scope() as session:
            user = upsert_user(session, userinfo)
            _establish_session(user, groups, id_token=token.get("id_token"))

        return RedirectResponse(app.storage.user.pop("redirect_to", "/"))

    @app.get("/logout")
    async def logout():
        id_token = app.storage.user.get("id_token")
        app.storage.user.clear()
        if settings.oidc_post_logout_redirect and id_token and settings.oidc_configured:
            try:
                meta = await oauth.authentik.load_server_metadata()
                end = meta.get("end_session_endpoint")
            except Exception:
                end = None
            if end:
                params = urlencode({
                    "id_token_hint": id_token,
                    "post_logout_redirect_uri": settings.oidc_post_logout_redirect,
                })
                return RedirectResponse(f"{end}?{params}")
        return RedirectResponse("/login")


def _establish_session(user: User, groups: list[str], id_token: str | None = None) -> None:
    app.storage.user.update({
        "authenticated": True,
        "user_id": user.id,
        "sub": user.sub,
        "name": user.display_name,
        "email": user.email,
        "groups": groups,
    })
    if id_token:
        app.storage.user["id_token"] = id_token


# ─── Gating middleware ───────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated users to /login, preserving their target."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            app.storage.user.get("authenticated")
            or path in UNRESTRICTED_ROUTES
            or path.startswith("/_nicegui")
        ):
            return await call_next(request)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?redirect_to={quote(target, safe='')}")
