"""Source registry — decouples the pipeline from YouTube (roadmap feature 02).

A ``SourceSpec`` describes everything the pipeline needs to know that is
*source-specific*: how to recognise a URL, which yt-dlp extractor-args to use,
whether the per-user cookie / PO-token plumbing applies, whether artist mode is
available, and whether fetched cover art needs a square crop. Adding a new
source (SoundCloud in feature 06, later Bandcamp) is then a single registry
entry instead of edits scattered across ``pipeline.py``.

Parity note: this module is the single source of truth for the YouTube host
matching and the ``EXTRACTOR_ARGS`` string that used to live in ``pipeline.py``.
The value is unchanged, so the frozen flag lists (and thus tag output) stay
byte-identical — the pipeline options snapshot test proves it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse

# YouTube player clients — verbatim from the value that lived in pipeline.py.
# android_vr serves the real bestaudio token-free (wins format selection); mweb is
# the cookie-capable client that downloads age-restricted tracks with a PO token.
# The pipeline imports this under its old name ``EXTRACTOR_ARGS`` so the frozen
# _ALBUM_FLAGS / _SINGLE_FLAGS reference it unchanged (metadata parity).
EXTRACTOR_ARGS_YT = "youtube:player_client=android_vr,mweb"

# YouTube hosts we accept; everything else is rejected before yt-dlp runs.
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}


@dataclass(frozen=True)
class SourceSpec:
    """One downloadable source and its source-specific behaviour."""

    key: str                               # stable id, e.g. "youtube"
    label: str                             # human-readable, e.g. "YouTube Music"
    extractor_args: str | None             # yt-dlp --extractor-args string, or None
    supports_cookies: bool                 # per-user cookie file applies
    supports_pot: bool                     # bgutil PO-token provider applies
    supports_artist: bool                  # artist-mode discography enumeration available
    cover_square_crop: bool                # thumbnails may be 16:9 → crop to square
    matches: Callable[[str], bool]         # True if this source handles the URL
    suggest_mode: Callable[[str], str | None]  # best-guess download mode from the URL
    # Trust the uploader/channel as the performing artist (roadmap 06). SoundCloud has no
    # structured artist credit (uploader IS the artist in the common case); YouTube must NOT
    # trust it (label re-uploads), so this defaults False and only SoundCloud sets it True.
    trust_uploader_as_artist: bool = False


def _matches_youtube(raw: str) -> bool:
    """True only for http(s) URLs on a known YouTube host (verbatim from the old is_supported_url)."""
    try:
        parsed = urlparse((raw or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def _suggest_mode_youtube(raw: str) -> str | None:
    """Best-guess download mode from a YouTube URL's shape (see roadmap spec table).

    | URL shape                                   | mode      |
    |---------------------------------------------|-----------|
    | ``?list=OLAK5uy_…`` (album playlist id)     | album     |
    | ``watch?v=…`` / ``youtu.be/…`` without list | single    |
    | ``playlist?list=PL…`` / ``RD…`` / other list| playlist  |
    | ``/channel/…`` , ``/@handle``               | artist    |
    | anything else                               | None      |
    """
    try:
        p = urlparse((raw or "").strip())
    except ValueError:
        return None
    path = p.path or ""
    host = (p.hostname or "").lower()
    query = parse_qs(p.query or "")
    list_id = (query.get("list") or [""])[0]

    # Artist channel / handle — checked first, a channel URL never carries a track list.
    if "/channel/" in path or "/@" in path:
        return "artist"

    # A list id decides album vs. playlist regardless of the /watch vs /playlist path.
    if list_id:
        return "album" if list_id.startswith("OLAK5uy_") else "playlist"

    # A single video: /watch?v=… or a youtu.be short link, both without a list.
    if path.rstrip("/").endswith("/watch") and query.get("v"):
        return "single"
    if host == "youtu.be" and len(path.strip("/")) > 0:
        return "single"

    return None


# SoundCloud hosts (roadmap 06). ``on.soundcloud.com`` is the share short-link.
_SOUNDCLOUD_HOSTS = {
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com",
}

# Profile-tab path segments that identify an ARTIST page (not a single track).
_SC_ARTIST_TABS = {"tracks", "albums", "sets", "popular-tracks", "toptracks"}
# Tabs we do not download in this iteration (a user's likes/reposts aren't their own catalogue).
_SC_REJECTED_TABS = {"likes", "reposts", "comments", "following", "followers"}


def _matches_soundcloud(raw: str) -> bool:
    """True only for http(s) URLs on a known SoundCloud host."""
    try:
        parsed = urlparse((raw or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in _SOUNDCLOUD_HOSTS


def _suggest_mode_soundcloud(raw: str) -> str | None:
    """Best-guess download mode from a SoundCloud URL's shape (roadmap 06 spec table).

    | URL shape                                   | mode      |
    |---------------------------------------------|-----------|
    | ``/<user>/sets/<slug>``                     | album     |
    | ``/<user>/<track>``                         | single    |
    | ``/<user>`` or ``/<user>/tracks|/albums|…`` | artist    |
    | ``/<user>/likes`` , ``/<user>/reposts``     | None      |
    | ``on.soundcloud.com/<short>`` , anything else | None    |

    The ``on.soundcloud.com`` short link resolves to an arbitrary target, so its shape is
    unknown from the URL → no suggestion (the user picks a mode).
    """
    try:
        p = urlparse((raw or "").strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host == "on.soundcloud.com":
        return None
    segs = [s for s in (p.path or "").split("/") if s]
    if not segs:
        return None
    tab = segs[1].lower() if len(segs) >= 2 else ""
    if tab in _SC_REJECTED_TABS:
        return None
    if tab == "sets":
        # /<user>/sets/<slug> is one set (album); /<user>/sets alone is the sets tab (artist).
        return "album" if len(segs) >= 3 else "artist"
    if len(segs) == 1 or tab in _SC_ARTIST_TABS:
        return "artist"
    if len(segs) == 2:
        return "single"
    # A private track share link carries a secret token as a third segment:
    # /<user>/<track>/s-XXXXXXXX — still one track.
    if len(segs) == 3 and segs[2].lower().startswith("s-"):
        return "single"
    return None


def _matches_bandcamp(raw: str) -> bool:
    """True only for http(s) URLs on a bandcamp.com host (roadmap 11).

    Bandcamp gives every artist a subdomain (``<artist>.bandcamp.com``), so the match is a
    SUFFIX check, not set membership: the host must equal ``bandcamp.com`` or end in
    ``.bandcamp.com``. That rejects lookalikes safely — ``bandcamp.com.evil.com`` (ends in
    ``.evil.com``) and ``evilbandcamp.com`` (no leading dot) both fail. Custom artist domains
    (CNAMEs) are intentionally NOT matched this iteration (spec: host-based detection only).
    """
    try:
        parsed = urlparse((raw or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host == "bandcamp.com" or host.endswith(".bandcamp.com")


def _suggest_mode_bandcamp(raw: str) -> str | None:
    """Best-guess download mode from a Bandcamp URL's shape (roadmap 11 spec table).

    | URL shape                                   | mode      |
    |---------------------------------------------|-----------|
    | ``<artist>.bandcamp.com/track/<slug>``      | single    |
    | ``<artist>.bandcamp.com/album/<slug>``      | album     |
    | ``<artist>.bandcamp.com`` / ``…/music``     | artist    |
    | anything else                               | None      |
    """
    try:
        p = urlparse((raw or "").strip())
    except ValueError:
        return None
    segs = [s for s in (p.path or "").split("/") if s]
    if not segs or segs[0].lower() == "music":
        return "artist"
    kind = segs[0].lower()
    if kind == "track":
        return "single"
    if kind == "album":
        return "album"
    return None


YOUTUBE = SourceSpec(
    key="youtube",
    label="YouTube Music",
    extractor_args=EXTRACTOR_ARGS_YT,
    supports_cookies=True,
    supports_pot=True,
    supports_artist=True,
    cover_square_crop=True,
    matches=_matches_youtube,
    suggest_mode=_suggest_mode_youtube,
)

SOUNDCLOUD = SourceSpec(
    key="soundcloud",
    label="SoundCloud",
    # yt-dlp handles SoundCloud natively — no extractor-args, no PO tokens, no cookie
    # plumbing. extractor_args=None makes _apply_source strip the youtube: pair for this source.
    extractor_args=None,
    supports_cookies=False,
    supports_pot=False,
    supports_artist=True,
    cover_square_crop=True,
    # SoundCloud has no structured artist credit tag — the uploader IS the artist in the
    # common case, so artist-mode crediting trusts it (YouTube must not; see SourceSpec).
    trust_uploader_as_artist=True,
    matches=_matches_soundcloud,
    suggest_mode=_suggest_mode_soundcloud,
)

BANDCAMP = SourceSpec(
    key="bandcamp",
    label="Bandcamp",
    # yt-dlp handles Bandcamp natively — no extractor-args, no PO tokens, no cookie plumbing.
    extractor_args=None,
    supports_cookies=False,
    supports_pot=False,
    supports_artist=True,
    cover_square_crop=True,
    # The page owner is the artist in the overwhelming case; a label selling third-party
    # releases still carries a proper `artist` tag, which the credit-tag-first order in
    # `_artist_credit_text` prefers — so trusting the uploader is safe (roadmap 11).
    trust_uploader_as_artist=True,
    matches=_matches_bandcamp,
    suggest_mode=_suggest_mode_bandcamp,
)

# The registry. Detection walks it in order, first match wins.
_REGISTRY: tuple[SourceSpec, ...] = (YOUTUBE, SOUNDCLOUD, BANDCAMP)


def detect_source(url: str) -> SourceSpec | None:
    """Return the SourceSpec that handles ``url``, or None for an unknown/invalid host."""
    for spec in _REGISTRY:
        if spec.matches(url):
            return spec
    return None


def is_supported_url(raw: str) -> bool:
    """True if any registered source handles the URL (re-exported by pipeline for call sites)."""
    return detect_source(raw) is not None


def suggest_mode(url: str) -> str | None:
    """Best-guess download mode for ``url`` from its detected source, or None."""
    spec = detect_source(url)
    return spec.suggest_mode(url) if spec else None
