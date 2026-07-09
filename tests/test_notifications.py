"""Notification dispatch (issue #42): channel senders, guards, and no-secret payloads."""
from types import SimpleNamespace

import pytest

from app import notifications
from app.notifications import NotifyConfig


def _cfg(**over) -> NotifyConfig:
    """A NotifyConfig with every event on and no channel — override what a test needs."""
    base = dict(
        language="en",
        notify_new_tracks=True, notify_sync_error=True, notify_download_error=True,
        ntfy_url="", ntfy_token="", webhook_url="",
        email_to="", smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
        smtp_from="", smtp_security="starttls",
    )
    base.update(over)
    return NotifyConfig(**base)


class _FakeResp:
    def __init__(self, status: int = 200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _capture_post(monkeypatch, resp: _FakeResp | None = None):
    """Replace httpx.post with a recorder; returns the list of (url, kwargs) calls."""
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return resp or _FakeResp()

    monkeypatch.setattr(notifications.httpx, "post", fake_post)
    return calls


# --- URL guard ----------------------------------------------------------------

def test_valid_http_url_scheme():
    assert notifications._valid_http_url("https://ntfy.sh/topic")
    assert notifications._valid_http_url("http://host:4416/x")
    assert not notifications._valid_http_url("file:///etc/passwd")
    assert not notifications._valid_http_url("ftp://host/x")
    assert not notifications._valid_http_url("")
    assert not notifications._valid_http_url("https://")  # no host


def test_valid_http_url_allowlist(monkeypatch):
    monkeypatch.setattr(notifications.settings, "notify_allowed_hosts", "ntfy.sh")
    assert notifications._valid_http_url("https://ntfy.sh/topic")
    assert not notifications._valid_http_url("https://evil.example/topic")


# --- Event guards -------------------------------------------------------------

def test_new_tracks_disabled_sends_nothing(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg(notify_new_tracks=False, ntfy_url="https://ntfy.sh/t")
    assert notifications.notify_new_tracks(cfg, playlist="Chill", count=3) == []
    assert calls == []


def test_new_tracks_zero_count_sends_nothing(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg(ntfy_url="https://ntfy.sh/t")
    assert notifications.notify_new_tracks(cfg, playlist="Chill", count=0) == []
    assert calls == []


def test_no_channel_configured_returns_empty(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg()  # every toggle on, but no channel
    assert notifications.notify_new_tracks(cfg, playlist="Chill", count=2) == []
    assert calls == []


def test_error_uses_matching_toggle(monkeypatch):
    _capture_post(monkeypatch)
    # sync error suppressed, download error allowed → only the download kind fires.
    cfg = _cfg(notify_sync_error=False, notify_download_error=True,
               webhook_url="https://hook.example/x")
    assert notifications.notify_error(cfg, kind="sync", url="u", error="boom") == []
    assert notifications.notify_error(cfg, kind="download", url="u", error="boom") == ["Webhook"]


# --- ntfy ---------------------------------------------------------------------

def test_ntfy_send_builds_url_headers_and_body(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg(ntfy_url="https://ntfy.sh/my-topic")
    sent = notifications.notify_new_tracks(cfg, playlist="Road Trip", count=3)
    assert sent == ["ntfy"]
    url, kw = calls[0]
    assert url == "https://ntfy.sh/my-topic"
    assert kw["headers"]["Title"] == "Soundpull"
    assert kw["headers"]["Priority"] == "default"
    assert kw["headers"]["Tags"] == "arrow_down"
    body = kw["content"].decode("utf-8")
    assert "Road Trip" in body and "3" in body


def test_ntfy_bearer_only_when_token(monkeypatch):
    calls = _capture_post(monkeypatch)
    notifications.notify_new_tracks(_cfg(ntfy_url="https://ntfy.sh/t"), playlist="P", count=1)
    assert "Authorization" not in calls[0][1]["headers"]

    calls2 = _capture_post(monkeypatch)
    notifications.notify_new_tracks(
        _cfg(ntfy_url="https://ntfy.sh/t", ntfy_token="tok123"), playlist="P", count=1)
    assert calls2[0][1]["headers"]["Authorization"] == "Bearer tok123"


def test_ntfy_invalid_url_is_swallowed_in_dispatch(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg(ntfy_url="file:///etc/passwd")  # blocked scheme
    assert notifications.notify_new_tracks(cfg, playlist="P", count=1) == []
    assert calls == []  # never reached httpx


# --- webhook ------------------------------------------------------------------

def test_webhook_payload_carries_no_secrets(monkeypatch):
    calls = _capture_post(monkeypatch)
    # Secrets present on the config must NOT appear in the webhook body (issue #42).
    cfg = _cfg(webhook_url="https://hook.example/x", ntfy_token="SECRET_TOKEN",
               smtp_password="SMTP_PW")
    notifications.notify_error(cfg, kind="sync", url="https://youtu.be/abc", error="fail")
    payload = calls[0][1]["json"]
    assert payload["event"] == "error"
    assert payload["kind"] == "sync"
    assert payload["url"] == "https://youtu.be/abc"
    blob = str(payload)
    assert "SECRET_TOKEN" not in blob and "SMTP_PW" not in blob


def test_error_text_truncated(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg(webhook_url="https://hook.example/x")
    notifications.notify_error(cfg, kind="download", url="u", error="x" * 900)
    assert len(calls[0][1]["json"]["error"]) <= notifications._ERROR_MAXLEN


# --- e-mail -------------------------------------------------------------------

class _FakeSMTP:
    last: "list[_FakeSMTP]" = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        _FakeSMTP.last.append(self)

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg

    def quit(self):
        pass


class _FakeSMTPSSL(_FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.last = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    return _FakeSMTP


def test_email_starttls_login_and_send(smtp):
    cfg = _cfg(email_to="me@example.org", smtp_host="smtp.example.org", smtp_port=587,
               smtp_user="u", smtp_password="p", smtp_from="from@example.org",
               smtp_security="starttls")
    sent = notifications.notify_new_tracks(cfg, playlist="P", count=2)
    assert sent == ["E-Mail"]
    inst = smtp.last[-1]
    assert type(inst) is _FakeSMTP           # plain SMTP, not SSL
    assert inst.started_tls is True
    assert inst.logged_in == ("u", "p")
    assert inst.sent["To"] == "me@example.org"
    assert inst.sent["From"] == "from@example.org"


def test_email_ssl_skips_starttls(smtp):
    cfg = _cfg(email_to="me@example.org", smtp_host="smtp.example.org", smtp_port=465,
               smtp_security="ssl")
    notifications.notify_new_tracks(cfg, playlist="P", count=1)
    inst = smtp.last[-1]
    assert type(inst) is _FakeSMTPSSL        # SSL transport
    assert inst.started_tls is False         # no STARTTLS on an already-encrypted socket


def test_email_from_falls_back_to_recipient(smtp):
    cfg = _cfg(email_to="me@example.org", smtp_host="h", smtp_security="none")
    notifications.notify_new_tracks(cfg, playlist="P", count=1)
    assert smtp.last[-1].sent["From"] == "me@example.org"


# --- dispatch resilience + test button ----------------------------------------

def test_dispatch_swallows_channel_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(notifications.httpx, "post", boom)
    cfg = _cfg(ntfy_url="https://ntfy.sh/t")
    # A failing channel is logged and swallowed — the event helper never raises.
    assert notifications.notify_new_tracks(cfg, playlist="P", count=1) == []


def test_send_test_ignores_toggles(monkeypatch):
    calls = _capture_post(monkeypatch)
    cfg = _cfg(notify_new_tracks=False, notify_sync_error=False,
               notify_download_error=False, ntfy_url="https://ntfy.sh/t")
    assert notifications.send_test(cfg) == ["ntfy"]
    assert calls[0][1]["headers"]["Title"] == "Soundpull: test"


def test_send_test_raises_on_channel_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad host")

    monkeypatch.setattr(notifications.httpx, "post", boom)
    cfg = _cfg(webhook_url="https://hook.example/x")
    with pytest.raises(RuntimeError):
        notifications.send_test(cfg)


def test_send_test_no_channel_returns_empty(monkeypatch):
    _capture_post(monkeypatch)
    assert notifications.send_test(_cfg()) == []


# --- config snapshot ----------------------------------------------------------

def test_from_settings_decrypts_secrets(monkeypatch):
    monkeypatch.setattr(notifications, "decrypt_secret", lambda tok: f"dec({tok})")
    us = SimpleNamespace(
        language="de",
        notify_new_tracks=1, notify_sync_error=0, notify_download_error=1,
        notify_ntfy_url="  https://ntfy.sh/t  ", notify_ntfy_token_enc="ENC1",
        notify_webhook_url="https://hook/x",
        notify_email_to="me@x.org", notify_smtp_host="smtp.x", notify_smtp_port=25,
        notify_smtp_user="u", notify_smtp_password_enc="ENC2",
        notify_smtp_from="from@x", notify_smtp_security="SSL",
    )
    cfg = NotifyConfig.from_settings(us)
    assert cfg.language == "de"
    assert cfg.notify_new_tracks is True and cfg.notify_sync_error is False
    assert cfg.ntfy_url == "https://ntfy.sh/t"      # trimmed
    assert cfg.ntfy_token == "dec(ENC1)"            # decrypted
    assert cfg.smtp_password == "dec(ENC2)"
    assert cfg.smtp_security == "ssl"               # lowercased
    assert cfg.smtp_port == 25


def test_from_settings_no_secrets_leaves_blank(monkeypatch):
    # A missing enc field must NOT call decrypt (no key needed for an empty config).
    monkeypatch.setattr(notifications, "decrypt_secret",
                        lambda tok: pytest.fail("decrypt should not be called"))
    us = SimpleNamespace(
        language=None,
        notify_new_tracks=0, notify_sync_error=0, notify_download_error=0,
        notify_ntfy_url=None, notify_ntfy_token_enc=None, notify_webhook_url=None,
        notify_email_to=None, notify_smtp_host=None, notify_smtp_port=None,
        notify_smtp_user=None, notify_smtp_password_enc=None, notify_smtp_from=None,
        notify_smtp_security=None,
    )
    cfg = NotifyConfig.from_settings(us)
    assert cfg.ntfy_token == "" and cfg.smtp_password == ""
    assert cfg.smtp_port == 587                     # default when None
    assert cfg.smtp_security == "starttls"
    assert cfg.language == "en" or cfg.language == "de"  # DEFAULT_LANGUAGE
