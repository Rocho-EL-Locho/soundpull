"""Per-user notifications for background events (issue #42).

Best-effort push/webhook/e-mail alerts when an interval-sync finds new tracks or when
a job fails. Modelled on `app/lyrics.py`: every network call is wrapped so a failure is
logged and swallowed — a notification must NEVER fail or block the download/sync that
triggered it.

Three channels, any combination of which a user configures in Settings:
  - **ntfy** — a single HTTP POST to a topic URL (self-hosted friendly, no SMTP).
  - **generic webhook** — a JSON POST, for wiring into anything.
  - **e-mail** — via the stdlib `smtplib` (no extra dependency).

Security:
  - The payload NEVER carries a secret (ntfy token / SMTP password / YouTube cookie /
    WebDAV password) — only the human message plus playlist/count/mode/url/error fields.
  - ntfy/webhook URLs must be http(s) and (optionally) pass the `NOTIFY_ALLOWED_HOSTS`
    allowlist — a minimal SSRF guard mirroring the WebDAV one.
  - Secrets (ntfy token, SMTP password) are Fernet-encrypted at rest and only decrypted
    into a `NotifyConfig` snapshot built inside a DB session (never exposed to the client).

The worker runs off any request/session context, so strings are resolved with an
EXPLICIT language via `app.i18n.translate` (the notification target's owner's language,
snapshotted into `NotifyConfig`) rather than the session-scoped `t()`.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.i18n import DEFAULT_LANGUAGE, translate
from app.security import decrypt_secret

log = logging.getLogger("notifications")

# Short budgets: a slow/unreachable channel must not hold a worker thread for long.
_TIMEOUT = 10
# Error text is truncated before it enters any payload (avoid huge/odd messages).
_ERROR_MAXLEN = 500

# Display names for channels (used in the "test sent to: …" feedback). Product/protocol
# names, so not translated.
_NTFY = "ntfy"
_WEBHOOK = "Webhook"
_EMAIL = "E-Mail"


@dataclass(frozen=True)
class NotifyConfig:
    """Immutable per-user snapshot: event toggles + channel config + language.

    Built INSIDE a DB session via `from_settings` (so secrets are decrypted while the
    `UserSettings` row is live), then passed to the off-thread dispatch. Holds decrypted
    secrets, so it never leaves the server.
    """
    language: str
    notify_new_tracks: bool
    notify_sync_error: bool
    notify_download_error: bool
    ntfy_url: str
    ntfy_token: str          # decrypted; "" when unset
    webhook_url: str
    email_to: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str       # decrypted; "" when unset
    smtp_from: str
    smtp_security: str       # "starttls" | "ssl" | "none"

    @classmethod
    def from_settings(cls, us) -> "NotifyConfig":
        """Snapshot a `UserSettings` row into a config (decrypting secrets)."""
        return cls(
            language=(getattr(us, "language", None) or DEFAULT_LANGUAGE),
            notify_new_tracks=bool(us.notify_new_tracks),
            notify_sync_error=bool(us.notify_sync_error),
            notify_download_error=bool(us.notify_download_error),
            ntfy_url=(us.notify_ntfy_url or "").strip(),
            ntfy_token=(decrypt_secret(us.notify_ntfy_token_enc)
                        if us.notify_ntfy_token_enc else ""),
            webhook_url=(us.notify_webhook_url or "").strip(),
            email_to=(us.notify_email_to or "").strip(),
            smtp_host=(us.notify_smtp_host or "").strip(),
            smtp_port=int(us.notify_smtp_port or 587),
            smtp_user=(us.notify_smtp_user or "").strip(),
            smtp_password=(decrypt_secret(us.notify_smtp_password_enc)
                           if us.notify_smtp_password_enc else ""),
            smtp_from=(us.notify_smtp_from or "").strip(),
            smtp_security=(us.notify_smtp_security or "starttls").strip().lower(),
        )


def _valid_http_url(url: str) -> bool:
    """True iff `url` is an http(s) URL whose host passes the optional allowlist.

    Blocks non-http schemes (file://, gopher://, …) and, when `NOTIFY_ALLOWED_HOSTS`
    is configured, any host outside it — a minimal SSRF guard for user-supplied targets.
    """
    try:
        parts = urlparse(url or "")
    except Exception:  # noqa: BLE001 - a malformed URL is simply invalid
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    allow = settings.notify_host_allowlist
    return not allow or parts.hostname.lower() in allow


def _truncate(text: str, limit: int = _ERROR_MAXLEN) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _email_configured(cfg: NotifyConfig) -> bool:
    return bool(cfg.smtp_host and cfg.email_to)


def _any_channel(cfg: NotifyConfig) -> bool:
    """True if at least one delivery channel has its required fields set."""
    return bool(cfg.ntfy_url or cfg.webhook_url or _email_configured(cfg))


# --- Channel senders (each validates + raises on failure; callers catch) -------

def _send_ntfy(cfg: NotifyConfig, title: str, message: str, priority: str,
               tags: list[str]) -> None:
    if not _valid_http_url(cfg.ntfy_url):
        raise ValueError(f"invalid/blocked ntfy URL: {cfg.ntfy_url!r}")
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    resp = httpx.post(cfg.ntfy_url, content=message.encode("utf-8"), headers=headers,
                      timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()


def _send_webhook(cfg: NotifyConfig, *, event: str, title: str, message: str,
                  priority: str, tags: list[str], data: dict) -> None:
    if not _valid_http_url(cfg.webhook_url):
        raise ValueError(f"invalid/blocked webhook URL: {cfg.webhook_url!r}")
    # Deliberately only non-secret fields — no token/password/cookie ever leaves here.
    payload = {"event": event, "title": title, "message": message,
               "priority": priority, "tags": tags, **data}
    resp = httpx.post(cfg.webhook_url, json=payload, timeout=_TIMEOUT,
                      follow_redirects=True)
    resp.raise_for_status()


def _send_email(cfg: NotifyConfig, title: str, message: str) -> None:
    if not _email_configured(cfg):
        raise ValueError("SMTP host and recipient are required")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg.smtp_from or cfg.smtp_user or cfg.email_to
    msg["To"] = cfg.email_to
    # RFC 5322 requires a Date; smtplib.send_message adds neither Date nor Message-ID, and a
    # missing Date makes strict MTAs/spam filters reject or penalise the mail.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(message)
    if cfg.smtp_security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT)
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT)
    try:
        if cfg.smtp_security == "starttls":
            smtp.starttls()
        if cfg.smtp_user and cfg.smtp_password:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001 - closing errors are irrelevant to delivery
            pass


def _dispatch(cfg: NotifyConfig, *, event: str, title: str, message: str,
              priority: str, tags: list[str], data: dict,
              raise_errors: bool = False) -> list[str]:
    """Fan `(title, message)` out to every configured channel. Best-effort.

    Returns the display names of channels that accepted the message. Per-channel errors
    are logged and swallowed, UNLESS `raise_errors` is set (used by the Settings "test"
    button, which surfaces a misconfiguration to the user) — then they are collected and
    re-raised together after every channel has been attempted.
    """
    senders: list[tuple[str, object]] = []
    if cfg.ntfy_url:
        senders.append((_NTFY, lambda: _send_ntfy(cfg, title, message, priority, tags)))
    if cfg.webhook_url:
        senders.append((_WEBHOOK, lambda: _send_webhook(
            cfg, event=event, title=title, message=message,
            priority=priority, tags=tags, data=data)))
    if _email_configured(cfg):
        senders.append((_EMAIL, lambda: _send_email(cfg, title, message)))

    sent: list[str] = []
    errors: list[tuple[str, Exception]] = []
    for name, fn in senders:
        try:
            fn()  # type: ignore[operator]
            sent.append(name)
        except Exception as exc:  # noqa: BLE001 - a channel failure must never propagate
            log.warning("notification via %s failed: %s", name, exc)
            errors.append((name, exc))
    if raise_errors and errors:
        raise RuntimeError("; ".join(f"{n}: {e}" for n, e in errors))
    return sent


# --- Public event API ----------------------------------------------------------

def notify_new_tracks(cfg: NotifyConfig, *, playlist: str, count: int) -> list[str]:
    """Notify that an interval-sync added `count` new tracks to `playlist`."""
    if not cfg.notify_new_tracks or count <= 0 or not _any_channel(cfg):
        return []
    title = translate(cfg.language, "notify.new_tracks_title")
    message = translate(cfg.language, "notify.new_tracks_body",
                        playlist=playlist or "?", count=count)
    return _dispatch(cfg, event="new_tracks", title=title, message=message,
                     priority="default", tags=["arrow_down"],
                     data={"playlist": playlist, "count": count})


def notify_error(cfg: NotifyConfig, *, kind: str, url: str, error: str) -> list[str]:
    """Notify that a job failed. `kind` is "sync" or "download" (each its own toggle)."""
    toggle = cfg.notify_sync_error if kind == "sync" else cfg.notify_download_error
    if not toggle or not _any_channel(cfg):
        return []
    kind_label = translate(cfg.language, f"notify.kind_{kind}")
    err = _truncate(error)
    title = translate(cfg.language, "notify.error_title")
    message = translate(cfg.language, "notify.error_body", kind=kind_label, error=err)
    return _dispatch(cfg, event="error", title=title, message=message,
                     priority="high", tags=["warning"],
                     data={"kind": kind, "url": url, "error": err})


def send_test(cfg: NotifyConfig) -> list[str]:
    """Send a test notification to every configured channel, ignoring the event toggles.

    Returns the channels that accepted it; raises `RuntimeError` if any configured channel
    failed (so the Settings page can show the reason). Raises nothing when no channel is
    configured — it just returns an empty list.
    """
    title = translate(cfg.language, "notify.test_title")
    message = translate(cfg.language, "notify.test_body")
    return _dispatch(cfg, event="test", title=title, message=message,
                     priority="default", tags=["bell"], data={}, raise_errors=True)
