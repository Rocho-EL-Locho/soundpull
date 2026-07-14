"""Library page pure helpers (roadmap 03): scan-age text + Navidrome deep link.

The page's data logic lives in `app.library_index` (covered by test_library_index); here we
only pin the two pure presentation helpers that have branching worth guarding.
"""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from app.i18n import t
from app.pages.library import _navidrome_album_url, _scanned_text


def _ago(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


def test_scanned_text_never():
    assert _scanned_text(None) == t("library.scanned_never")


def test_scanned_text_recent_under_one_hour():
    assert _scanned_text(_ago(minutes=10)) == t("library.scanned_recent")


def test_scanned_text_hours():
    assert _scanned_text(_ago(hours=3, minutes=1)) == t("library.scanned_hours", hours=3)


def test_scanned_text_days():
    assert _scanned_text(_ago(days=2, hours=1)) == t("library.scanned_days", days=2)


def test_scanned_text_naive_datetime_treated_as_utc():
    # A naive (SQLite) timestamp must not raise on the tz-aware subtraction.
    naive = _ago(hours=5).replace(tzinfo=None)
    assert _scanned_text(naive) == t("library.scanned_hours", hours=5)


def test_navidrome_album_url_encodes_name_and_trims_slash():
    url = _navidrome_album_url("https://music.host/", "Best Of")
    assert url.startswith("https://music.host/app/#/album?filter=")   # trailing slash trimmed
    query = url.split("filter=", 1)[1]
    assert json.loads(urllib.parse.unquote(query)) == {"name": "Best Of"}


def test_navidrome_album_url_rejects_non_http_scheme():
    # A javascript:/data: base would be a self-XSS as an <a href> → refuse (empty = no link).
    assert _navidrome_album_url("javascript:alert(1)", "X") == ""
    assert _navidrome_album_url("data:text/html,x", "X") == ""
    assert _navidrome_album_url("", "X") == ""
    assert _navidrome_album_url("http://music.host", "X").startswith("http://music.host/app/")
