"""Download pipeline — yt-dlp (as a library) + cover fetch + Navidrome tagging + WebDAV.

Metadata parity with the original bash scripts is guaranteed by building the
*identical* yt-dlp CLI flag list and converting it with `yt_dlp.parse_options()`
into the options dict that `YoutubeDL` consumes. We only add progress hooks on
top — the postprocessor/metadata behaviour is exactly what the CLI produced.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import subprocess
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
import yt_dlp

from app import fix_music_tags, lyrics
from app.config import settings

log = logging.getLogger("pipeline")

# Staging area for downloads (ZIP packaging + WebDAV work tree). In-memory job
# state does not survive a restart, so anything left here is orphaned — see
# purge_work_root(), called once at startup.
_WORK_ROOT = Path(settings.local_music_root) / ".work"

# YouTube hosts we accept; everything else is rejected before yt-dlp runs.
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}


def purge_work_root() -> None:
    """Delete leftover staging dirs/ZIPs from previous runs (call at startup)."""
    shutil.rmtree(_WORK_ROOT, ignore_errors=True)


def _write_cookie_file(job_id: str, cookies_txt: str | None) -> Path | None:
    """Persist a user's decrypted cookies.txt to a 0600 file for yt-dlp's `cookiefile`.

    Kept OUTSIDE the per-job work dir on purpose: the WebDAV delivery mirrors the
    whole work tree, so a cookie inside it would be uploaded. Returns the path, or
    None when no cookie is given (issue #9). The caller removes it after the job.
    """
    if not cookies_txt:
        return None
    _WORK_ROOT.mkdir(parents=True, exist_ok=True)
    path = _WORK_ROOT / f"{job_id}.cookies.txt"
    # Create the file 0600 atomically (not write-then-chmod) so the secret is never
    # briefly world-readable at the prevailing umask. job_id is a uuid, so O_EXCL
    # never trips in practice; unlink first defends against a stale leftover.
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(cookies_txt)
    return path


def is_supported_url(raw: str) -> bool:
    """True only for http(s) URLs on a known YouTube host."""
    try:
        parsed = urlparse((raw or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


# Chars a Windows-backed / oCIS (OpenCloud) WebDAV server rejects in a path segment even when
# percent-encoded — a raw "?" in an album folder made oCIS return 400 and skip the upload
# (issue #56). Map them to yt-dlp's fullwidth look-alikes (yt-dlp already uses these for the
# track *filenames*, so our folders and its files match); path separators stay "_" as before so
# existing folder names are unchanged.
_SEGMENT_MAP = str.maketrans({
    "/": "_", "\\": "_", "\x00": "",
    "?": "？", "*": "＊", ":": "：", '"': "＂", "<": "＜", ">": "＞", "|": "｜",
})


def _safe_segment(name: str) -> str:
    """Make a metadata string safe as a single path segment (no traversal, server-safe chars)."""
    seg = name.translate(_SEGMENT_MAP).strip()
    return "Unbekannt" if seg in ("", ".", "..") else seg

# Verbatim from download_album.sh / download_single.sh.
EXTRACTOR_ARGS = "youtube:player_client=ios,web,android;-android_sdkless"

# yt-dlp flags for the download step, identical to the bash scripts (genre and
# -o output template are appended per-run). --ignore-config keeps it deterministic.
_ALBUM_FLAGS = [
    "--ignore-config",
    "--audio-quality", "320K",
    "--embed-metadata",
    "--write-playlist-metafiles",
    "-x",
    "--audio-format", "mp3",
    "--embed-thumbnail",
    "--convert-thumbnails", "jpg",
    "--parse-metadata", "playlist_index:%(track_number)s",
    "--add-metadata",
    "--extractor-args", EXTRACTOR_ARGS,
    "-f", "bestaudio/best",
]
_SINGLE_FLAGS = [
    "--ignore-config",
    "--audio-quality", "320K",
    "--embed-metadata",
    "-x",
    "--audio-format", "mp3",
    "--embed-thumbnail",
    "--convert-thumbnails", "jpg",
    "--add-metadata",
    "--extractor-args", EXTRACTOR_ARGS,
    "-f", "bestaudio/best",
]

# Selectable audio quality / format (issue #10). Single source of truth.
#   key -> (yt-dlp --audio-format codec | None, --audio-quality value | None)
#
# YouTube serves lossy audio (~128-160 kbps Opus/AAC), which is the real quality
# ceiling. We therefore expose only the tiers that add distinct value:
#   - original : copy the source stream (Opus/M4A) without re-encoding — best
#                fidelity AND smallest file; the right choice unless a device
#                can't play Opus.
#   - mp3_320  : transparent transcode for maximum compatibility; also the
#                historical default, so its flag list is a no-op transform on
#                the lists above → output stays byte-identical (metadata parity).
#   - mp3_192  : compatible like 320 but ~40% smaller; still near-transparent
#                for a ~160 kbps source.
# Deliberately omitted: mp3_256 (redundant between 320/192) and mp3_128 (below
# the source bitrate → audibly worse for little gain — "original" covers small).
# Fallback artist name when `enumerate_artist` can't resolve one from the channel.
# Kept as a named sentinel so callers can tell "unknown" apart from a real name (issue #56:
# the compilation filter must NOT run against an unresolved name — it would drop everything).
_UNKNOWN_ARTIST = "Artist"

DEFAULT_AUDIO_FORMAT = "mp3_320"
AUDIO_FORMATS: dict[str, tuple[str | None, str | None]] = {
    "mp3_320": ("mp3", "320K"),
    "mp3_192": ("mp3", "192K"),
    "original": (None, None),
}
# Localized labels for the UI selects live in app.i18n (keys "audio.<format>"),
# built from these keys via app.i18n.audio_format_labels().

# Filename template for playlist tracks (issue #11): a REAL playlist keeps every
# track in one folder (the playlist name) with an .m3u8 that Navidrome imports as a
# playlist. The `%(playlist_index)04d` prefix preserves playlist order and avoids
# collisions between same-titled tracks. yt-dlp sanitises `%(title)s` into a safe
# segment (verified: '/' → '⧸'); the folder name is our own `_safe_segment(title)`.
_PLAYLIST_TRACK_TMPL = "%(playlist_index)04d - %(title)s.%(ext)s"


def _playlist_folder_name(title: str, playlist_id: str) -> str:
    """Folder segment for a delivered playlist, disambiguated by its id (issue #39).

    Two different playlists can share a title ("Chill"). Since the delivery folder AND
    its `.m3u8` are named after the title, same-named playlists would land in the same
    `<webdav>/Chill/` folder and the second delivery would overwrite the first's manifest
    (and clobber tracks). Appending the stable playlist id (`… [PLxxxx]`) keeps each
    playlist in its own folder. The id is stable per URL, so an interval-sync (issue #21)
    keeps targeting the same folder. Falls back to the bare title when no id is known.
    """
    name = f"{title} [{playlist_id}]" if playlist_id else title
    return _safe_segment(name)


def normalize_audio_format(value: str | None) -> str:
    """Return a known audio-format key, falling back to the default."""
    return value if value in AUDIO_FORMATS else DEFAULT_AUDIO_FORMAT


def audio_format_short(value: str | None) -> str:
    """Compact label for lists/history, e.g. 'MP3 320' or 'Original'."""
    codec, quality = AUDIO_FORMATS[normalize_audio_format(value)]
    return "Original" if codec is None else f"{codec.upper()} {(quality or '').rstrip('K')}"


def _apply_audio_format(flags: list[str], audio_format: str) -> list[str]:
    """Return a copy of `flags` with codec/quality set per `audio_format`.

    For the default (`mp3_320`) this is a no-op — same codec, same bitrate —
    so the produced flag list (and thus tag output) is unchanged.
    """
    codec, quality = AUDIO_FORMATS[normalize_audio_format(audio_format)]
    out = list(flags)

    qi = out.index("--audio-quality")
    if quality is not None:
        out[qi + 1] = quality
    else:
        del out[qi:qi + 2]

    fi = out.index("--audio-format")
    if codec is not None:
        out[fi + 1] = codec
    else:
        del out[fi:fi + 2]

    return out


def _genre_flags(genre: str | None) -> list[str]:
    """`--postprocessor-args` to force a genre, or `[]` for "no genre" (issue #21).

    An empty/blank genre skips the override so each track keeps its own metadata genre
    (useful for mixed-artist playlists). A real genre still forces it exactly as before,
    so the default path is unchanged (parity). Gated additionally by `tag_options.genre`
    at the call site — off there strips the genre entirely via `fix_music_tags`.
    """
    g = (genre or "").strip()
    return ["--postprocessor-args", f"ffmpeg:-metadata genre={g}"] if g else []


def _apply_tag_options(flags: list[str], options: fix_music_tags.TagOptions) -> list[str]:
    """Return a copy of `flags` with download-time fields gated per `options`.

    Drops the thumbnail-embed flags when cover is off and the playlist→track
    remap when track numbers are off. With all options on this is a no-op, so the
    flag list (and thus tag output) is unchanged — the parity baseline. (Genre is
    gated where the postprocessor-arg is appended in `run_download`; the remaining
    fields are stripped post-download in fix_music_tags.)
    """
    out = list(flags)
    if not options.cover:
        if "--embed-thumbnail" in out:
            out.remove("--embed-thumbnail")
        if "--convert-thumbnails" in out:
            ti = out.index("--convert-thumbnails")
            del out[ti:ti + 2]
    if not options.track_number and "--parse-metadata" in out:
        pi = out.index("--parse-metadata")
        del out[pi:pi + 2]
    return out


@dataclass
class Destination:
    type: str = "browser"  # browser (ZIP for download) | webdav (direct upload)
    webdav_url: str | None = None
    webdav_folder: str | None = None  # chosen target sub-folder (relative to base)
    webdav_username: str | None = None
    webdav_password: str | None = None  # decrypted


@dataclass
class Result:
    summary: str = ""
    zip_path: str | None = None   # set for browser destination
    zip_name: str | None = None
    # (artist, title, rel_path) of every track uploaded to WebDAV — fed to the server
    # index (issue #21/#31). `rel_path` is the file's path relative to the WebDAV base
    # folder, stored so a later playlist can reference it. Populated for all modes; the
    # caller records it only for WebDAV.
    delivered: list = field(default_factory=list)
    new_track_count: int = 0      # tracks actually downloaded (relevant for sync)
    # Updated ordered m3u manifest for a playlist SYNC (see `existing_tracks`); the
    # caller persists it on the subscription to rebuild the complete playlist next run.
    playlist_files: list = field(default_factory=list)
    playlist_name: str = ""       # resolved playlist title (for the subscription UI)


@dataclass
class Reporter:
    on_phase: Callable[[str], None] = field(default=lambda phase: None)
    on_meta: Callable[[str, str], None] = field(default=lambda artist, album: None)
    on_track: Callable[[int, int], None] = field(default=lambda cur, tot: None)
    # Album-level progress for an artist run (issue #32): (current, total, album name).
    # No-op by default so album/single/playlist callers are unaffected.
    on_album: Callable[[int, int, str], None] = field(default=lambda cur, tot, name: None)


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): log.warning("yt-dlp: %s", msg)
    def error(self, msg): log.error("yt-dlp: %s", msg)


def _build_ydl_opts(flags: list[str]) -> dict:
    """Convert a CLI flag list into a YoutubeDL options dict (parity-safe)."""
    return yt_dlp.parse_options(flags).ydl_opts


def _extractor_args() -> dict:
    return _build_ydl_opts(["--extractor-args", EXTRACTOR_ARGS]).get("extractor_args", {})


def _apply_cookie_policy(opts: dict, cookiefile: str | None) -> None:
    """Pin yt-dlp's cookie source to the user's own cookie — never the server's.

    Sets `cookiefile` to the per-user cookie file (or None → no cookie at all) and
    forces `cookiesfrombrowser=None`, so yt-dlp never falls back to a browser cookie
    store on the server. `--ignore-config` already blocks a yt-dlp config file from
    injecting `--cookies`/`--cookies-from-browser`; this makes the "user cookie or
    nothing" rule explicit and resistant to future drift (issue #9)."""
    opts["cookiefile"] = cookiefile      # the user's cookie, or None → no cookie
    opts["cookiesfrombrowser"] = None    # never read a browser cookie store on the server


def _primary_artist(raw: str | None) -> str:
    """Main artist = part before the first ', ' (mirrors `sed 's/, .*//'`)."""
    if not raw or raw == "NA":
        return "Unbekannt"
    return raw.split(", ")[0].strip() or "Unbekannt"


def _probe_meta(url: str, is_album: bool, cookiefile: str | None = None) -> tuple[str | None, str | None]:
    """Read artist/album from the first item (like `yt-dlp --simulate --print`)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": _extractor_args(),
        "logger": _QuietLogger(),
    }
    _apply_cookie_policy(opts, cookiefile)
    if is_album:
        opts["playlist_items"] = "1"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entry = info
    if info and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        entry = entries[0] if entries else info
    artist = (entry or {}).get("artist") or (entry or {}).get("uploader")
    album = (entry or {}).get("album")
    return artist, album


def _probe_playlist(url: str, cookiefile: str | None = None) -> tuple[str, str, int, str]:
    """Read a playlist's title, uploader, entry count and id (issue #11 / #39).

    Uses `extract_flat` so we only touch the playlist envelope, not every video —
    fast, and enough to name the download and seed the progress total. A playlist
    spans many artists/albums, so (unlike `_probe_meta`) there is no single
    artist/album to collapse to; each track is tagged from its own metadata later.
    Returns (title, uploader, count, playlist_id); count is 0 when unknown and the
    id is "" when the extractor exposes none. The id disambiguates the delivery
    folder so two same-named playlists don't collide (issue #39).
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "extractor_args": _extractor_args(),
        "logger": _QuietLogger(),
    }
    _apply_cookie_policy(opts, cookiefile)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    title = info.get("title") or "Playlist"
    uploader = info.get("uploader") or info.get("channel") or "Playlist"
    entries = [e for e in (info.get("entries") or []) if e]
    count = info.get("playlist_count") or len(entries)
    playlist_id = info.get("id") or info.get("playlist_id") or ""
    return title, uploader, int(count or 0), str(playlist_id)


def enumerate_playlist_tracks(url: str, cookiefile: str | None = None,
                              limit: int = 0) -> list[tuple[str, str]]:
    """(artist, title) for each playlist entry, metadata only — NO download (issue #21).

    Seeds the server index for a "mark existing" subscription first run: a per-entry
    (non-flat) extraction so artist/track are the real tags, not the sparse flat
    fields. `limit` caps entries (0 = unlimited). Mirrors the artist/title fallbacks
    used by the sync match-filter so the seeded keys line up with later lookups.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # A single unavailable/region-locked entry must not abort enumerating the whole
        # playlist — skip it (it appears as a None entry) and keep going (issue #21).
        "ignoreerrors": True,
        "extractor_args": _extractor_args(),
        "logger": _QuietLogger(),
    }
    _apply_cookie_policy(opts, cookiefile)
    if limit and limit > 0:
        opts["playlistend"] = limit
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    pairs: list[tuple[str, str]] = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        title = entry.get("track") or entry.get("title") or ""
        artists = entry.get("artists")
        first = artists[0] if isinstance(artists, list) and artists else ""
        artist = entry.get("artist") or first or entry.get("uploader") or ""
        if title:
            pairs.append((artist, title))
    return pairs


def enumerate_artist(url: str, cookiefile: str | None = None,
                     limit: int = 0) -> tuple[str, list[dict]]:
    """Artist name + every release for an artist/channel URL, metadata only (issue #32).

    A YouTube Music artist's whole catalogue (albums, EPs AND singles) surfaces via the
    channel's `/releases` tab, where each release is an `OLAK5uy_…` album playlist. We flat-probe
    the given URL to resolve its `channel_url`, then flat-extract `<channel_url>/releases`. Every
    release is downloaded through the normal album path, so a single lands as a 1-track album
    folder (faithful to how YT Music / Navidrome model it). `limit` caps the number of releases
    (0 = unlimited). Returns `(artist_name, [{"title", "url"}, …])`.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        # A dead/region-locked release must not abort enumerating the whole discography.
        "ignoreerrors": True,
        "extractor_args": _extractor_args(),
        "logger": _QuietLogger(),
    }
    _apply_cookie_policy(opts, cookiefile)

    releases_url = url if url.rstrip("/").endswith("/releases") else None
    if releases_url is None:
        with yt_dlp.YoutubeDL({**opts, "playlistend": 1}) as ydl:
            probe = ydl.extract_info(url, download=False) or {}
        channel_url = (probe.get("channel_url") or probe.get("uploader_url")
                       or probe.get("webpage_url") or url)
        releases_url = channel_url.rstrip("/") + "/releases"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(releases_url, download=False) or {}
    artist = (info.get("channel") or info.get("uploader")
              or (info.get("title") or "").removesuffix(" - Releases") or _UNKNOWN_ARTIST)
    releases: list[dict] = []
    for entry in (info.get("entries") or []):
        if entry and entry.get("url"):
            releases.append({"title": entry.get("title") or "Album", "url": entry["url"]})
    if limit and limit > 0:
        releases = releases[:limit]
    return artist, releases


def pick_square_cover(thumbnails: list[dict] | None) -> str | None:
    """Largest square thumbnail; prefer signed (sqp=) URLs (verbatim logic)."""
    signed_best = None
    signed_size = 0
    any_best = None
    any_size = 0
    for t in thumbnails or []:
        w = t.get("width", 0) or 0
        h = t.get("height", 0) or 0
        u = t.get("url", "")
        if w != h:
            continue
        if "sqp=" in u and w > signed_size:
            signed_best, signed_size = u, w
        if w > any_size:
            any_best, any_size = u, w
    return signed_best or any_best


def _download_cover(url: str) -> bytes | None:
    """GET a cover image URL; None on any failure (cover is best-effort)."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001 - never fail the job over a cover
        log.warning("cover fetch failed: %s", exc)
        return None


# Thumbnail image formats yt-dlp may leave on disk (--write-thumbnail).
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _square_crop_jpeg(src: Path) -> bytes | None:
    """Center-crop an image to a square and return JPEG bytes (via ffmpeg).

    A 16:9 YouTube thumbnail would otherwise be padded with blurred bars by
    Navidrome; cropping to `min(w,h)²` fills the square. An already-square source
    (YouTube Music art) is unchanged by the crop. Returns None on any failure so
    the caller falls back to the embedded thumbnail. ffmpeg is a hard dependency.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", "crop=min(iw\\,ih):min(iw\\,ih)", "-q:v", "2", "-f", "mjpeg", "pipe:1"],
            capture_output=True, check=True,
        )
        return proc.stdout or None
    except Exception as exc:  # noqa: BLE001 - cover is best-effort; keep embedded
        log.warning("cover square-crop failed: %s", exc)
        return None


def _fetch_cover(url: str, is_album: bool, dest: Path, cookiefile: str | None = None) -> Path | None:
    """Download the square album cover into `dest` (cover.jpg). Returns path or None."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _QuietLogger()}
    _apply_cookie_policy(opts, cookiefile)
    if is_album:
        opts["extract_flat"] = True  # playlist-level thumbnails (the album art)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:  # cover is best-effort; embedded thumbnail remains
        log.warning("cover probe failed: %s", exc)
        return None
    cover_url = pick_square_cover((data or {}).get("thumbnails"))
    if not cover_url:
        return None
    data_bytes = _download_cover(cover_url)
    if data_bytes is None:
        return None
    dest.write_bytes(data_bytes)
    return dest


def _track_meta(path: Path) -> tuple[str, str, int]:
    """(title, artist, duration_seconds) read from a track's tags via mutagen.

    Best-effort for the `.m3u8` #EXTINF lines; falls back to the filename stem and
    an unknown (-1) duration so a track with sparse tags still lists cleanly.
    """
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return path.stem, "", -1
        title = (audio.get("title") or [""])[0]
        artist = (audio.get("artist") or [""])[0]
        dur = int(getattr(audio.info, "length", 0) or 0)
        return title or path.stem, artist, (dur or -1)
    except Exception:  # noqa: BLE001 - m3u metadata is cosmetic; never fail the job
        return path.stem, "", -1


def _m3u_line_safe(text: str) -> str:
    """Collapse any CR/LF/control chars to spaces so a value can't inject an m3u line.

    The m3u format is line-oriented; a newline inside a playlist/track title (or the
    `#PLAYLIST:` name) would otherwise forge extra directive/track lines.
    """
    return "".join(" " if ord(ch) < 32 else ch for ch in text).strip()


def _write_m3u(folder: Path, name: str, tracks: list[Path]) -> Path:
    """Write an `<name>.m3u8` (UTF-8) into `folder`, listing `tracks` in order.

    Tracks are referenced by bare filename (relative to the playlist file). Navidrome
    auto-imports `.m3u`/`.m3u8` files found in the library and resolves those relative
    paths against the file's own folder, so the download becomes a real playlist
    (issue #11) — distinct from an album, tracks keep their own metadata.
    """
    lines = ["#EXTM3U", f"#PLAYLIST:{_m3u_line_safe(name)}"]
    for path in tracks:
        title, artist, dur = _track_meta(path)
        head = f"{artist} - {title}" if artist else title
        lines.append(f"#EXTINF:{dur},{_m3u_line_safe(head)}")
        lines.append(_m3u_line_safe(path.name))
    m3u_path = folder / f"{_safe_segment(name)}.m3u8"
    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path


# Leading zero-padded playlist index in a track filename ("0001 - Title.mp3").
_TRACK_INDEX = re.compile(r"^\s*(\d{1,4})\s*-")


def _index_from_name(name: str) -> int:
    """Playlist index parsed from a track filename ("0003 - X.mp3" → 3), else 0."""
    m = _TRACK_INDEX.match(name)
    return int(m.group(1)) if m else 0


def _m3u_entries_from_paths(tracks: list[Path]) -> list[dict]:
    """Build ordered m3u manifest entries (index/name/title/artist/dur) from files."""
    entries: list[dict] = []
    for path in tracks:
        title, artist, dur = _track_meta(path)
        entries.append({"index": _index_from_name(path.name), "name": path.name,
                        "title": title, "artist": artist, "dur": dur})
    return entries


def _write_m3u_entries(folder: Path, name: str, entries: list[dict]) -> Path:
    """Write `<name>.m3u8` from precomputed manifest entries, sorted by index/name.

    Unlike `_write_m3u` (which reads local files), this rebuilds the COMPLETE playlist
    on an incremental sync (issue #21) from entries that may no longer be on local
    disk — tracks referenced by bare filename resolve against the folder on the server.
    """
    ordered = sorted(entries, key=lambda e: (e.get("index") or 0, e.get("name", "")))
    lines = ["#EXTM3U", f"#PLAYLIST:{_m3u_line_safe(name)}"]
    for entry in ordered:
        title = entry.get("title") or entry.get("name", "")
        artist = entry.get("artist") or ""
        head = f"{artist} - {title}" if artist else title
        lines.append(f"#EXTINF:{entry.get('dur', -1)},{_m3u_line_safe(head)}")
        lines.append(_m3u_line_safe(entry.get("name", "")))
    m3u_path = folder / f"{_safe_segment(name)}.m3u8"
    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path


def _merge_manifest(existing: list[dict] | None, new: list[dict]) -> list[dict]:
    """Union of prior + new manifest entries, deduped by filename (new wins)."""
    by_name: dict[str, dict] = {e["name"]: e for e in (existing or []) if e.get("name")}
    for entry in new:
        if entry.get("name"):
            by_name[entry["name"]] = entry
    return list(by_name.values())


def _entry_key(entry: dict) -> tuple[str, str]:
    """Normalised (artist, title) of a manifest entry — matches the server-index key."""
    from app.library_index import track_key

    return track_key(entry.get("title", ""), entry.get("artist", ""))


def _build_playlist_manifest(existing: list[dict] | None, new_entries: list[dict],
                             ref_entries: list[dict], is_sync: bool) -> list[dict]:
    """Combine downloaded + referenced (+ prior, for sync) tracks into one m3u manifest.

    A cross-folder reference (issue #31) is dropped when the same track — by normalised
    `(artist, title)` — is already downloaded this run or present in the prior manifest:
    the in-folder copy wins, so a track is never listed twice. `_write_m3u_entries` orders
    the result by playlist index. Non-sync one-shot downloads have no prior manifest.
    """
    have = {_entry_key(e) for e in new_entries}
    if is_sync:
        have |= {_entry_key(e) for e in (existing or [])}
    refs = [e for e in ref_entries if _entry_key(e) not in have]
    combined = new_entries + refs
    return _merge_manifest(existing, combined) if is_sync else combined


def _artist_credit_text(info_dict: dict) -> str:
    """Lower-cased blob of a track's real *credit* tags (issue #56).

    Uses ONLY the tag fields that name a performer — `artists`/`artist`/`creators`/`creator`/
    `album_artist`. Deliberately EXCLUDES:
    - `title`/`track`/`album` — a label upload whose *title* merely mentions the artist
      ("<Artist> - <Song> - <Label>") must NOT count as crediting them; and
    - `channel`/`uploader` — the *upload source*, not a credit. YouTube Music surfaces old,
      broken self-uploads on the artist's OWN channel (channel="BCee") whose actual metadata is
      empty (`artist=None`, performer only in the free-text title); those defeat dedup and land
      mis-tagged exactly like a foreign label upload, so the crediting channel must not save them.

    A cleanly-tagged own release always carries a real `artist`/`artists` tag, so this blob is
    populated for the tracks we want and empty for the broken ones we don't.
    """
    parts: list[str] = []
    for key in ("artists", "creators"):
        val = info_dict.get(key)
        if isinstance(val, list):
            parts += [str(x) for x in val if x]
    for key in ("artist", "creator", "album_artist"):
        val = info_dict.get(key)
        if val:
            parts.append(str(val))
    return re.sub(r"\s+", " ", " , ".join(parts)).casefold()


def _norm_name(text: str) -> str:
    """Casefold + collapse whitespace — the shared normal form for artist/title matching."""
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _credits_artist(info_dict: dict, artist: str) -> bool:
    """True if `artist` is a credited performer of the track (issue #56).

    Word-boundary match against `_artist_credit_text`, so "BCee" matches "BCee, Charlotte
    Haining" but NOT "Spearhead Records" (label-as-artist) and NOT a longer word that merely
    contains it. A track with NO real credit tag (`artist=None` etc. — the broken video-name
    uploads the `/releases` tab mixes in) yields an empty blob and is therefore NOT credited →
    skipped. A blank target never filters (returns True) so a run with no known artist is a no-op.
    """
    target = _norm_name(artist)
    if not target:
        return True
    return re.search(rf"(?<!\w){re.escape(target)}(?!\w)", _artist_credit_text(info_dict)) is not None


# Trailing non-title suffixes a label upload's video name carries (dropped when repairing).
_VIDEO_NAME_SUFFIX = re.compile(
    r"\s*[\(\[]\s*(?:official(?:\s+(?:music\s+)?(?:video|audio|visuali[sz]er))?|"
    r"music\s+video|lyric(?:s)?(?:\s+video)?|visuali[sz]er|audio|hd|hq|4k|"
    r"free\s+download|out\s+now|premiere)\s*[\)\]]\s*$", re.IGNORECASE)

def _repair_broken_title(raw_title: str, own_artist: str) -> tuple[str, str] | None:
    """Parse a label-upload video name back into ``(artist, title)`` — issue #56.

    Many artist-mode `/releases` entries are third-party/label uploads tagged with NO artist
    and the whole video name as the title: ``<Artist> - <Song>[ - <Label>]`` (e.g.
    ``BCee & Lomax - Brazilian Wax - Spearhead Records``). Rather than drop these (they'd
    otherwise be filtered as "not credited"), we recover clean tags — but ONLY when the artist
    prefix (before the first `` - ``) credits the artist we're downloading (`own_artist`) as one
    of its individual names. That both confirms the track is theirs and anchors the split, so a
    foreign upload whose name merely CONTAINS the artist (``Wax Tailor - …`` for own_artist
    "Wax") is left alone — `own_artist` must equal a whole prefix artist, not a substring.

    Returns ``(artist, title)`` — the ``<Artist> - `` prefix and a trailing `` - <Label>``
    segment removed, plus a trailing video-name suffix like ``(Official Video)``; a ``feat.``
    clause is LEFT in the title for `fix_music_tags`. Returns None when it doesn't match (a
    clean ``title="Colours"`` has no `` - ``; a name not crediting own_artist).

    Label stripping: a trailing segment is dropped as the label UNLESS it starts with a bracket
    (a version like ``(LSB remix)``; labels rarely start with one), which re-attaches to the
    title — so ``So Right - (LSB remix) - Spearhead Records`` → ``So Right (LSB remix)`` and
    ``So Right - (LSB remix)`` keeps the remix. Heuristic (a non-bracketed real trailing
    segment — a co-artist, a subtitle — can be lost), but far better than a raw video name.
    """
    if not raw_title or " - " not in raw_title:
        return None
    target = _norm_name(own_artist)
    if not target:
        return None
    prefix, _, rest = raw_title.partition(" - ")
    # Split the prefix into individual credited artists (feat./ft. → separator) and require
    # own_artist to be EXACTLY one of them — so "Wax" doesn't match the artist "Wax Tailor".
    prefix_norm = re.sub(r"\b(?:featuring|feat|ft)\b\.?", "&", prefix, flags=re.IGNORECASE)
    prefix_artists = fix_music_tags.split_artists(prefix_norm)
    if not any(_norm_name(a) == target for a in prefix_artists):
        return None
    segs = [s.strip() for s in rest.split(" - ") if s.strip()]
    if not segs:
        return None
    # Drop a trailing label segment unless it's a bracketed version like "(LSB remix)"; then
    # rejoin, attaching a bracketed segment with a space so it reads as part of the title.
    if len(segs) >= 2 and segs[-1][:1] not in "([":
        segs = segs[:-1]
    title = segs[0]
    for seg in segs[1:]:
        title += (" " if seg[:1] in "([" else " - ") + seg
    title = _VIDEO_NAME_SUFFIX.sub("", title).strip()
    if not title:
        return None
    artist = " / ".join(prefix_artists) or prefix.strip()
    return artist, title


def _save_easy_tags(mf) -> None:
    """Save an ``easy=True`` mutagen file, keeping MP3 on ID3v2.3.

    mutagen's ``EasyMP3.save()`` defaults to writing ID3v2.4, but `fix_music_tags` writes v2.3;
    an artist-mode tag rewrite (`_repair_album_titles` / `_unify_album_year`) must stay on v2.3
    so one album folder doesn't end up with mixed ID3 versions. Other formats save normally.
    """
    from mutagen.mp3 import EasyMP3

    if isinstance(mf, EasyMP3):
        mf.save(v2_version=3)
    else:
        mf.save()


def _repair_album_titles(album_dir: Path, own_artist: str) -> None:
    """Rewrite label-upload video-name tags in an artist-mode album folder (issue #56).

    For a staged audio file that is NOT already credited to `own_artist` (a broken label upload)
    and whose title `_repair_broken_title` can recover, rewrite its title/artist tags and rename
    the file to the clean title, so `fix_music_tags` then normalises it like any clean track
    (feat cleanup, forced album_artist) and the delivered/indexed name is clean too. A track
    already crediting `own_artist` is CLEAN — its tags are authoritative and left untouched (even
    if its real title happens to look like `<Artist> - … - …`). Every original name is reserved
    up front so a rename can never overwrite another file. Tag-write and rename are coupled and
    best-effort: if the tag write fails the file is NOT renamed (name and tags stay consistent —
    never a clean filename over a still-raw title tag). Runs BEFORE `process_directory`.
    """
    from mutagen import File as MutagenFile

    audio = [p for p in sorted(album_dir.iterdir())
             if p.is_file() and p.suffix.lower() in fix_music_tags._SUPPORTED_EXTS]
    # Seed with EVERY original name so a rename can never clobber another file (a clean track,
    # or a broken one not yet processed) — Path.rename overwrites silently on POSIX.
    taken: set[str] = {p.name.casefold() for p in audio}
    for path in audio:
        try:
            mf = MutagenFile(str(path), easy=True)
        except Exception:  # noqa: BLE001 - unreadable file: leave it as-is
            mf = None
        if mf is None:
            continue
        cur_title = (mf.get("title") or [None])[0]
        cur_artist = (mf.get("artist") or [None])[0]
        # A track already credited to own_artist is CLEAN — its tags are authoritative, never
        # rewrite it (even if its real title happens to look like "<Artist> - … - …").
        if _credits_artist({"artist": cur_artist or ""}, own_artist):
            continue
        repaired = _repair_broken_title(cur_title or path.stem, own_artist)
        if repaired is None:
            continue
        new_artist, new_title = repaired
        try:
            if mf.tags is None:
                mf.add_tags()
            mf["title"] = [new_title]
            mf["artist"] = [new_artist]
            _save_easy_tags(mf)
        except Exception:  # noqa: BLE001 - tag write failed → skip rename too, so name and
            continue        # tags stay consistent (never a clean name over a raw title tag)
        # Rename to the clean title (collision-safe); the file's own name frees up first.
        taken.discard(path.name.casefold())
        base = _safe_segment(new_title) or path.stem
        candidate = f"{base}{path.suffix}"
        n = 2
        while candidate.casefold() in taken:
            candidate = f"{base} ({n}){path.suffix}"
            n += 1
        taken.add(candidate.casefold())
        if candidate != path.name:
            try:
                path.rename(path.with_name(candidate))
            except Exception:  # noqa: BLE001 - rename best-effort (tags already clean)
                taken.discard(candidate.casefold())
                taken.add(path.name.casefold())


def _unify_album_year(album_dir: Path) -> None:
    """Give every track in an artist-mode album folder the same (earliest) date (issue #56).

    Navidrome groups albums by (albumartist, album, date), so a label sampler whose tracks each
    carry their OWN original release year splits one folder into a separate album per year
    ("Volume Two" ×5). Forcing one date collapses it back to a single album. No-op for a real
    album (dates already uniform) or when fewer than two tracks carry a date. Best-effort per
    file; only runs in artist mode, so a plain album/single download is untouched (parity).
    """
    from mutagen import File as MutagenFile

    loaded = []
    dates: list[str] = []
    for p in sorted(album_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in fix_music_tags._SUPPORTED_EXTS:
            continue
        try:
            mf = MutagenFile(str(p), easy=True)
        except Exception:  # noqa: BLE001 - unreadable file: leave it as-is
            mf = None
        if mf is None:
            continue
        loaded.append(mf)
        d = (mf.get("date") or [None])[0]
        if d:
            dates.append(str(d))
    if len(dates) < 2 or len(set(dates)) < 2:
        return  # already uniform (a real album) or nothing to unify
    earliest = min(dates)  # YYYYMMDD / YYYY-MM-DD / YYYY all sort chronologically as strings
    for mf in loaded:
        if (mf.get("date") or [None])[0] != earliest:
            try:
                mf["date"] = [earliest]
                _save_easy_tags(mf)
            except Exception:  # noqa: BLE001 - best-effort
                pass


def _dedup_staged_tracks(work_base: Path) -> int:
    """Drop a redundant SINGLE staged for a track already in a real album (issue #56).

    The same recording often arrives both inside a multi-track album AND as a standalone single.
    For each ``(artist, title)`` that appears in more than one folder, if the biggest folder is a
    real album (>1 track) we delete only the copies that sit ALONE in a 1-track folder (a single),
    plus each removed track's sibling `.lrc`, and remove the emptied folder.

    Deliberately conservative: a copy that is itself in a multi-track album is NEVER deleted, so
    two different tracks that merely share a title across albums (an "Intro"/"Interlude"/"Outro"
    on each, live-vs-studio) are kept — `track_key` normalises away artist detail and ignores the
    album, so treating those as duplicates would silently delete distinct recordings. When every
    copy is a single (biggest folder is 1 track) none is deleted — we can't tell which is
    canonical. Runs once, single-threaded, after the parallel fan-out. Returns files removed.
    """
    from collections import defaultdict

    from app.library_index import track_key

    audio = [p for p in work_base.rglob("*")
             if p.is_file() and p.suffix.lower() in fix_music_tags._SUPPORTED_EXTS]
    folder_size: dict[Path, int] = defaultdict(int)
    for p in audio:
        folder_size[p.parent] += 1
    by_key: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in audio:
        title, art, _ = _track_meta(p)
        by_key[track_key(title, art)].append(p)

    removed = 0
    emptied: set[Path] = set()
    for key, paths in by_key.items():
        if not key[0] or not key[1] or len(paths) <= 1:  # need a real artist+title, and a dup
            continue
        if max(folder_size[p.parent] for p in paths) <= 1:
            continue  # every copy is a 1-track single → can't tell which is canonical; keep all
        for dup in paths:
            if folder_size[dup.parent] == 1:   # a lone single, and a bigger album has this track
                dup.unlink(missing_ok=True)
                dup.with_suffix(".lrc").unlink(missing_ok=True)
                emptied.add(dup.parent)
                removed += 1
    # A dropped single leaves its 1-track folder without audio → remove it (+ leftover cover).
    for d in emptied:
        if d.is_dir() and not any(f.suffix.lower() in fix_music_tags._SUPPORTED_EXTS
                                  for f in d.iterdir() if f.is_file()):
            shutil.rmtree(d, ignore_errors=True)
    return removed


def _make_match_filter(on_server: Callable[[str, str], bool] | None = None,
                       on_skip: Callable[[int | None, str, str], None] | None = None,
                       own_artist: str | None = None):
    """yt-dlp match_filter: skip tracks we don't want to download (issue #21/#31/#56).

    yt-dlp extracts each entry's full metadata (artist/track) BEFORE downloading the
    media, then calls this; returning a string skips the entry. `incomplete` is True
    during playlist enumeration when tags aren't final — we defer (return None) so the
    real check runs on the complete info_dict.

    Two independent skip reasons compose:

    - `own_artist` (issue #56, artist mode): reject a track whose credited artist does not
      include this name. A YouTube-Music artist's `/releases` tab mixes in compilations and
      third-party "appears-on" / label uploads whose artist tag is the LABEL (or is absent —
      the performer lives only in the video title). Those can never dedup against a cleanly
      tagged library and would land as mis-tagged duplicates, so a track that isn't credited
      to the artist we're downloading is dropped up front (checked before `on_server`).
      Exception: a broken upload whose video NAME still starts with the artist
      (`_repair_broken_title`) is KEPT — the pipeline repairs its tags after download instead
      of losing a genuine (if mis-tagged) release track.
    - `on_server` (issue #21/#31): reject a track already in the user's library. `on_skip`
      (issue #31) is then invoked once, on the effective (non-incomplete) call, for every
      such skip — so the pipeline can reference the already-present copy in a playlist's
      .m3u8. yt-dlp has merged a stable 1-based `playlist_index` into the info_dict by this
      point, even for skipped entries.
    """
    def _filter(info_dict: dict, incomplete: bool = False) -> str | None:
        if incomplete or info_dict.get("_type") in ("playlist", "multi_video"):
            return None
        title = info_dict.get("track") or info_dict.get("title") or ""
        artists = info_dict.get("artists")
        first = artists[0] if isinstance(artists, list) and artists else ""
        artist = info_dict.get("artist") or first or info_dict.get("uploader") or ""
        # A broken upload (not credited) is repaired iff its video name still starts with the
        # artist; if not even repairable, it's foreign → drop. `repaired` stays None for a
        # credited (clean) track, so its authoritative tags aren't second-guessed here.
        repaired = None
        if own_artist and not _credits_artist(info_dict, own_artist):
            repaired = _repair_broken_title(title, own_artist)
            if repaired is None:
                return f"nicht vom Künstler {own_artist}: {artist or '?'} - {title}".strip()
        # Dedup on the key the track will actually be INDEXED under: a repaired upload is filed
        # under own_artist + its recovered title (so a clean copy already on the server skips,
        # and the pre/post-download keys agree — issue #56); a normal track uses its raw meta.
        d_artist, d_title = (own_artist, repaired[1]) if repaired else (artist, title)
        if on_server is not None and d_title and on_server(d_artist, d_title):
            if on_skip is not None:
                on_skip(info_dict.get("playlist_index"), d_artist, d_title)
            return f"schon auf dem Server: {d_artist} - {d_title}".strip()
        return None
    return _filter


def _ensure_remote_dir(client, posix_dir: str) -> None:
    parts = [p for p in posix_dir.split("/") if p]
    cumulative = ""
    for part in parts:
        cumulative = f"{cumulative}/{part}" if cumulative else part
        try:
            if not client.exists(cumulative):
                client.mkdir(cumulative)
        except Exception:
            # Race / already-exists on some servers — verify and continue.
            if not client.exists(cumulative):
                raise


def _zip_dir(src_dir: Path, zip_path: Path, root_name: str) -> None:
    """Zip the contents of src_dir under a top-level folder `root_name`."""
    root_name = root_name.replace("/", "-").replace("\\", "-").strip() or "Album"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, f"{root_name}/{path.relative_to(src_dir).as_posix()}")


_UPLOAD_ATTEMPTS = 3


def _upload_with_retry(client, local: str, remote: str) -> None:
    """Upload one file, retrying TRANSIENT network failures (timeout / transport error).

    A single slow or dropped PUT during a big artist upload must not abort the whole job (which
    would discard every already-downloaded album); retry a few times with linear backoff. A
    non-transient error (bad path, auth, 4xx) is not retried — it re-raises immediately.
    """
    for attempt in range(1, _UPLOAD_ATTEMPTS + 1):
        try:
            client.upload_file(local, remote, overwrite=True)
            return
        except httpx.TransportError as exc:  # timeout / connect / read / write / network
            if attempt == _UPLOAD_ATTEMPTS:
                raise
            log.warning("WebDAV upload %r failed (attempt %d/%d): %s — retrying",
                        remote, attempt, _UPLOAD_ATTEMPTS, exc)
            time.sleep(2.0 * attempt)


def _upload_tree(dest: Destination, local_root: Path) -> list[str]:
    """Upload the staged tree to WebDAV; return the list of files that could NOT be uploaded.

    A single file the server rejects (e.g. HTTP 400 on a name it dislikes, after transient
    retries are exhausted) must not discard a whole discography upload — it's logged WITH its
    full remote path (so the offending name is diagnosable) and skipped, and the rest proceed.
    If NOT ONE file uploads, the failure is systemic (auth, bad base URL, unreachable) → raise so
    the job surfaces as an error rather than a silent empty upload.
    """
    from app.webdav_util import make_client

    client = make_client(dest.webdav_url, dest.webdav_username, dest.webdav_password)
    prefix = (dest.webdav_folder or "").strip("/")
    files = [p for p in sorted(local_root.rglob("*")) if p.is_file()]
    uploaded = 0
    failed: list[str] = []
    for path in files:
        rel = path.relative_to(local_root).as_posix()
        remote = f"{prefix}/{rel}" if prefix else rel
        parent = "/".join(remote.split("/")[:-1])
        try:
            if parent:
                _ensure_remote_dir(client, parent)
            _upload_with_retry(client, str(path), remote)
            uploaded += 1
        except Exception as exc:  # noqa: BLE001 - one rejected path must not lose the whole upload
            log.warning("WebDAV upload skipped %r: %s", remote, exc)
            failed.append(rel)
    if failed and uploaded == 0:
        raise RuntimeError(f"WebDAV-Upload fehlgeschlagen (0/{len(files)}): "
                           f"{failed[0]} — {'; '.join(failed[1:3])}".rstrip(" —"))
    if failed:
        log.warning("WebDAV: %d von %d Dateien übersprungen (Server lehnte den Pfad ab): %s",
                    len(failed), len(files), ", ".join(repr(f) for f in failed[:5]))
    return failed


def run_download(*, job_id: str, url: str, genre: str, mode: str,
                 destination: Destination, reporter: Reporter,
                 audio_format: str = DEFAULT_AUDIO_FORMAT,
                 tag_options: fix_music_tags.TagOptions = fix_music_tags.TagOptions(),
                 cookies_txt: str | None = None,
                 on_server: Callable[[str, str], bool] | None = None,
                 existing_ref: Callable[[str, str], str | None] | None = None,
                 existing_tracks: list[dict] | None = None,
                 stage_dir: Path | None = None, deliver: bool = True,
                 album_name: str | None = None,
                 own_artist: str | None = None,
                 fetch_lyrics: bool = False) -> Result:
    """Execute one download end-to-end and return a Result.

    Both destinations stage into a temp work dir; then either a ZIP is packaged
    (browser) or the tree is uploaded (webdav). Raises on fatal errors.

    `tag_options` gates which metadata fields are written (issue #7); the default
    (all on) keeps the output byte-identical to the original tool.

    `cookies_txt` is the user's decrypted Netscape cookies.txt (issue #9); when
    given it is handed to every yt-dlp call so bot-checks/age gates don't block
    the download. When omitted, no `cookiefile` is set — the output stays
    byte-identical (metadata parity).

    `mode` is one of `album` / `single` / `playlist`. A playlist (issue #11)
    spans many artists/albums, so — unlike album/single, which collapse to one
    forced artist+album — each track lands in and is tagged from its OWN
    metadata; unknown/legacy modes fall back to album.

    `on_server(artist, title) -> bool` enables DEDUP (issue #21/#31): it becomes a yt-dlp
    match_filter that skips tracks already on the server, so only new ones download (zero
    new tracks is then a normal, non-error outcome — for any mode). It is parity-safe: the
    filter only selects which entries download, never how the downloaded ones are tagged.
    When None (a plain download) the behaviour and output are unchanged.

    `existing_ref(artist, title) -> rel_path | None` (issue #31) resolves a skipped
    playlist track to the library-relative path of its existing copy; the pipeline then
    writes a cross-folder relative reference into the `.m3u8` so the playlist stays
    complete with no duplicate file (a skipped track with no known path is omitted, never
    re-downloaded). Ignored for album/single (no m3u). `existing_tracks` is a
    subscription's prior m3u manifest, merged with new+referenced tracks to rebuild the
    COMPLETE `<name>.m3u8`.

    `stage_dir` / `deliver` support the artist orchestrator (issue #32): when `stage_dir` is
    given, the run stages into THAT shared dir (instead of `_WORK_ROOT/job_id`) and the caller
    owns its lifetime — so this call must NOT remove it. With `deliver=False` the staged, tagged
    tree is left in place and the delivery step (ZIP / WebDAV upload) is skipped; the returned
    Result carries `delivered` so the caller can deliver the combined tree once. The download +
    tag steps in between are unchanged, so metadata parity is unaffected.

    `own_artist` (artist mode, issue #56) is the known performer of the run and does two things:
    (1) installs a match_filter that skips any track NOT credited to this artist — YouTube Music's
    `/releases` tab mixes an artist's own albums with third-party compilation / label uploads whose
    artist tag is the label (or absent), which would otherwise never dedup and land as mis-tagged
    duplicates; and (2) forces it as the album's primary artist (folder / `album_artist` tag /
    server-index key) so a release probed on a label channel isn't filed under the label. Only the
    artist orchestrator passes it (album/single/playlist name their source directly); None keeps
    the probed artist, so a plain download is unchanged (metadata parity).

    `album_name` (album mode, issue #32) forces the album folder + tag to a known release title
    instead of trusting each track's `%(album)s`. The artist orchestrator passes the release
    title from the `/releases` tab so a single (which carries no album tag) lands in its own
    `Artist/<Release>/` folder instead of collapsing every tag-less single into one shared
    `%(album)s`→"NA" folder (where they would cross-contaminate on tagging). None (a plain
    album/single download) keeps the original `%(album)s`/"Singles" behaviour → parity preserved.
    """
    is_playlist = mode == "playlist"
    is_single = mode == "single"
    is_album = not is_playlist and not is_single  # unknown/legacy modes → album
    is_sync = is_playlist and on_server is not None  # issue #21: playlist-with-dedup
    dedup = on_server is not None  # issue #21/#31: skip-if-present active for this run

    # Cross-folder m3u references only make sense for a WebDAV library; a browser ZIP has
    # no library and its tracks are packaged under a single root, so a `../` reference would
    # point outside the archive. Callers already gate dedup to WebDAV — this hardens it.
    if destination.type != "webdav":
        existing_ref = None

    # Materialise the cookie to a 0600 file (kept outside work_base so the WebDAV
    # delivery never ships it); cleaned up in `finally`. None when no cookie → the
    # no-cookie path is byte-identical (metadata parity).
    cookie_path: Path | None = None
    # An artist run stages many releases into ONE shared dir it owns (issue #32); a plain
    # run gets its own per-job dir that we clean up in `finally`.
    work_base = stage_dir if stage_dir is not None else _WORK_ROOT / job_id
    try:
        cookie_path = _write_cookie_file(job_id, cookies_txt)
        cookiefile = str(cookie_path) if cookie_path else None

        # 1) Metadata → output-directory template. Albums/singles collapse to one
        #    artist/album; a playlist keeps each track's own (issue #11). Always
        #    stage into a temp work dir; delivery then packages a ZIP (browser) or
        #    uploads the tree (webdav). The work dir is removed in `finally`.
        reporter.on_phase("metadata")
        work_base.mkdir(parents=True, exist_ok=True)
        pl_title = ""
        if is_playlist:
            pl_title, pl_uploader, pl_count, pl_id = _probe_playlist(url, cookiefile=cookiefile)
            if settings.max_playlist_items > 0 and pl_count:
                pl_count = min(pl_count, settings.max_playlist_items)
            reporter.on_meta(pl_uploader, pl_title)
            if pl_count:
                reporter.on_track(0, pl_count)
            # A real playlist: ALL tracks in one folder, plus an .m3u8 written after
            # tagging. The folder is named after the playlist but disambiguated by its
            # id so two same-named playlists don't collide (issue #39); the name is
            # interpolated literally, so `_playlist_folder_name` sanitises it into one
            # safe path segment (the track filename is a yt-dlp template, sanitised by
            # yt-dlp). The `.m3u8` inside keeps the plain `pl_title` for display.
            playlist_dir = work_base / _playlist_folder_name(pl_title, pl_id)
            out_tmpl = str(playlist_dir / _PLAYLIST_TRACK_TMPL)
            base_flags = _SINGLE_FLAGS  # per-track tags, no album track-number remap
        else:
            artist_raw, album_raw = _probe_meta(url, is_album, cookiefile=cookiefile)
            # In artist mode (`own_artist` set) force the known performer as the album's primary
            # artist (issue #56): releases on a label channel probe as artist=<label> (e.g.
            # "Drum&BassArena"), which would fold the whole album under the label in Navidrome and
            # split the discography by upload-source casing ("Bcee" vs "BCee"). We already know the
            # real artist for the run, so use it for the folder / album_artist / server-index key.
            # None for a plain album/single download → falls back to the probed artist (parity).
            primary_artist = own_artist or _primary_artist(artist_raw)
            album = (album_name or album_raw or "Unbekannt Album") if is_album else "Singles"
            reporter.on_meta(primary_artist, album)
            # `primary_artist` is interpolated literally (not a yt-dlp `%(...)s`
            # field), so sanitise it ourselves to keep it a single, traversal-safe
            # path segment. With a known `album_name` (artist mode) the album folder is
            # likewise a literal segment — so tag-less singles get distinct per-release
            # folders instead of collapsing into one `%(album)s`→"NA" directory.
            if is_album and album_name:
                subfolder = _safe_segment(album)
            else:
                subfolder = "%(album)s" if is_album else "Singles"
            out_tmpl = str(work_base / _safe_segment(primary_artist) / subfolder / "%(title)s.%(ext)s")
            base_flags = _ALBUM_FLAGS if is_album else _SINGLE_FLAGS

        # 2) Download (parity-safe opts from parse_options + our hooks).
        flags = _apply_audio_format(base_flags, audio_format)
        flags = _apply_tag_options(flags, tag_options)
        if tag_options.genre:
            flags += _genre_flags(genre)
        # Playlists keep the per-track thumbnail on disk (as jpg) so we can crop it
        # to a square cover afterwards (issue #11); album/single don't need it (they
        # embed one fetched square album cover). Only when the cover field is on —
        # else _apply_tag_options already dropped the thumbnail flags.
        if is_playlist and tag_options.cover:
            flags += ["--write-thumbnail"]
        flags += ["-o", out_tmpl]
        opts = _build_ydl_opts(flags)
        opts.update({"quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger()})
        _apply_cookie_policy(opts, cookiefile)
        if is_playlist and settings.max_playlist_items > 0:
            opts["playlistend"] = settings.max_playlist_items  # cap runaway playlists
        if is_playlist:
            # A playlist spans many videos; a dead/region-locked one must not abort the
            # whole run — skip it and download the rest (issue #21). Set on the opts dict
            # (not the frozen flag lists) so album/single stay strict and tag parity holds.
            opts["ignoreerrors"] = True
        # Dedup: skip tracks already on the server, capturing each skip so a playlist can
        # reference the existing copy afterwards (issue #21/#31).
        skipped: list[tuple[int | None, str, str]] = []  # (playlist_index, artist, title)
        if on_server is not None or own_artist:
            opts["match_filter"] = _make_match_filter(
                on_server, on_skip=lambda idx, a, t: skipped.append((idx, a, t)),
                own_artist=own_artist)

        finished_dirs: Counter[str] = Counter()

        def progress_hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                info = d.get("info_dict") or {}
                idx = info.get("playlist_index")
                total = info.get("n_entries") or info.get("playlist_count") or 0
                reporter.on_phase("download")
                if idx:
                    reporter.on_track(int(idx), int(total or 0))
            elif status == "finished":
                name = d.get("filename") or (d.get("info_dict") or {}).get("filepath")
                if name:
                    finished_dirs[str(Path(name).parent)] += 1

        opts["progress_hooks"] = [progress_hook]

        reporter.on_phase("download")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Resolve dedup-skipped playlist tracks into cross-folder .m3u8 references
        # (issue #31): a skipped track whose existing library path is known is listed at a
        # relative path from the playlist folder (a bare filename when it is already in the
        # same folder); one with no known path is left out — never re-downloaded.
        ref_entries: list[dict] = []
        if is_playlist and existing_ref is not None and skipped:
            playlist_rel = playlist_dir.relative_to(work_base).as_posix()
            for idx, artist, title in skipped:
                rel = existing_ref(artist, title)
                if not rel:
                    continue
                ref_entries.append({"index": int(idx or 0),
                                    "name": posixpath.relpath(rel, playlist_rel),
                                    "title": title, "artist": artist, "dur": -1})

        if not finished_dirs:
            if not dedup and not own_artist:
                raise RuntimeError("Download lieferte keine Dateien (siehe Logs).")
            # Dedup active (issue #21/#31) or a whole release filtered out as compilations /
            # label uploads (issue #56) — nothing NEW downloaded is a normal outcome.
            if is_playlist:
                manifest = _build_playlist_manifest(existing_tracks, [], ref_entries, is_sync)
                if ref_entries:
                    # New cross-folder references complete the playlist though nothing was
                    # downloaded: regenerate + upload just the .m3u8 (no duplicate files).
                    playlist_dir.mkdir(parents=True, exist_ok=True)
                    _write_m3u_entries(playlist_dir, pl_title, manifest)
                    if deliver and destination.type == "webdav":
                        reporter.on_phase("upload")
                        _upload_tree(destination, work_base)
                return Result(summary="Keine neuen Titel", new_track_count=0,
                              playlist_files=manifest, playlist_name=pl_title)
            return Result(summary="Keine neuen Titel", new_track_count=0)

        # 3) Navidrome tag correction (frozen fix_music_tags logic), gated per
        #    tag_options — all-on keeps the original behaviour. A playlist tags
        #    every track in the tree from its OWN metadata (no forced album/artist,
        #    no single shared cover); album/single force the one album + primary
        #    artist and embed the fetched square cover.
        reporter.on_phase("tags")
        if is_playlist:
            # Tag each track from its OWN metadata (no forced album/artist), embedding
            # a per-track SQUARE cover: yt-dlp left a `<stem>.jpg` thumbnail per track
            # (--write-thumbnail); we center-crop it to a square so Navidrome doesn't
            # pad a 16:9 thumbnail with blurred bars (already-square art is unchanged).
            # Then delete those stray thumbnails and write the .m3u8 so Navidrome sees
            # a playlist, not an album. process_tree returns the tracks in order.
            thumb_by_stem = {p.stem: p for p in playlist_dir.glob("*")
                             if p.is_file() and p.suffix.lower() in _IMAGE_EXTS}

            def cover_for(fp: str) -> bytes | None:
                if not tag_options.cover:
                    return None  # cover disabled → thumbnail already stripped
                src = thumb_by_stem.get(Path(fp).stem)
                return _square_crop_jpeg(src) if src else None  # else keep embedded

            tracks = [Path(p) for p in fix_music_tags.process_tree(
                str(playlist_dir), tag_options, cover_for=cover_for)]
            for src in thumb_by_stem.values():
                src.unlink(missing_ok=True)  # don't ship the standalone thumbnails
            # Record what we're delivering (issue #21/#31): (artist, title, rel_path) per
            # track, read from the FINAL tags so the server index matches later lookups;
            # rel_path (relative to the WebDAV base) lets a future playlist reference it.
            new_entries = _m3u_entries_from_paths(tracks)
            delivered = [(e["artist"], e["title"], p.relative_to(work_base).as_posix())
                         for p, e in zip(tracks, new_entries)]
            if dedup:
                # Rebuild the COMPLETE playlist: new downloads + cross-folder references to
                # already-present tracks (+ the prior manifest for a sync). We upload only
                # the new files (+ regenerated m3u8); referenced/prior tracks stay in place.
                manifest = _build_playlist_manifest(existing_tracks, new_entries, ref_entries, is_sync)
                _write_m3u_entries(playlist_dir, pl_title, manifest)
            else:
                manifest = new_entries
                _write_m3u(playlist_dir, pl_title, tracks)
            stage_root = playlist_dir
            root_name = pl_title
            webdav_label = pl_title
        else:
            album_dir = Path(finished_dirs.most_common(1)[0][0])
            if not album_dir.is_dir():
                # Defensive: keeps fix_music_tags' sys.exit path (BaseException) unreachable.
                raise RuntimeError(f"Album-Verzeichnis fehlt: {album_dir}")
            # Artist mode (issue #56): recover clean title/artist tags for label-upload video
            # names ("<Artist> - <Song> - <Label>") BEFORE tagging, so fix_music_tags normalises
            # them like any clean track instead of them shipping as mis-tagged duplicates.
            if own_artist:
                _repair_album_titles(album_dir, own_artist)
            cover_path = (_fetch_cover(url, is_album, album_dir / "cover.jpg", cookiefile=cookiefile)
                          if tag_options.cover else None)
            fix_music_tags.process_directory(
                str(album_dir),
                str(cover_path) if cover_path else None,
                album,
                primary_artist,
                tag_options,
            )
            # Artist mode (issue #56): a label sampler's tracks each carry their own release
            # year, which Navidrome would split into one album per year — force a single date.
            if own_artist:
                _unify_album_year(album_dir)
            # Record delivered tracks for the server index (issue #21/#31). Album/single
            # force one primary artist, so pair each track's title with it; rel_path
            # (relative to the WebDAV base) lets a future playlist reference it.
            audio_files = sorted(p for p in album_dir.glob("*")
                                 if p.suffix.lower() in fix_music_tags._SUPPORTED_EXTS)
            delivered = [(primary_artist, _track_meta(p)[0], p.relative_to(work_base).as_posix())
                         for p in audio_files]
            manifest = []
            stage_root = album_dir
            root_name = f"{primary_artist} - {album}"
            webdav_label = f"{primary_artist}/{album}"

        # Synced lyrics (issue #43): best-effort `.lrc` sidecars next to each track. Additive
        # and non-fatal — a miss/error just skips (never fails the job) and never touches the
        # frozen tag output. All modes; `.lrc` is neither an image nor audio ext, so it
        # survives every cleanup glob and is excluded from the m3u/server index (audio-only).
        # Runs before the `deliver` check so artist sub-runs stage sidecars into the shared
        # tree for the orchestrator to deliver too; both delivery paths ship whatever is staged.
        # Fetched concurrently (bounded pool) with a `lyrics` progress phase so a big playlist
        # doesn't serialise N blocking HTTP round-trips (the artist reporter swallows the phase).
        if fetch_lyrics:
            lyric_targets = sorted(p for p in stage_root.rglob("*")
                                   if p.suffix.lower() in fix_music_tags._SUPPORTED_EXTS)
            if lyric_targets:
                reporter.on_phase("lyrics")
                lyrics.write_lrc_sidecars(lyric_targets, progress=reporter.on_track)

        # Artist run (issue #32): the staged, tagged tree stays in the shared dir; the
        # orchestrator delivers the whole tree once. Hand back what we produced so it can
        # accumulate the delivered tracks and combine the delivery.
        if not deliver:
            return Result(summary="", delivered=delivered, new_track_count=len(delivered),
                          playlist_files=manifest, playlist_name=pl_title)

        # 4) Deliver. WebDAV mirrors the whole work tree into the library (for a
        #    playlist that's the `<name>/` folder with its tracks + .m3u8); browser
        #    ZIPs the staged folder under a single top-level name.
        if destination.type == "webdav":
            reporter.on_phase("upload")
            _upload_tree(destination, work_base)
            return Result(summary=f"WebDAV: {webdav_label}", delivered=delivered,
                          new_track_count=len(delivered), playlist_files=manifest,
                          playlist_name=pl_title)

        reporter.on_phase("packaging")
        zip_path = _WORK_ROOT / f"{job_id}.zip"
        _zip_dir(stage_root, zip_path, root_name)
        return Result(summary=f"{root_name}.zip", zip_path=str(zip_path), zip_name=f"{root_name}.zip",
                      delivered=delivered, new_track_count=len(delivered), playlist_files=manifest,
                      playlist_name=pl_title)
    finally:
        # Only remove the work dir we created; a shared `stage_dir` is the caller's to clean.
        if stage_dir is None:
            shutil.rmtree(work_base, ignore_errors=True)
        if cookie_path:
            cookie_path.unlink(missing_ok=True)


def run_artist_download(*, job_id: str, url: str, genre: str, destination: Destination,
                        reporter: Reporter, audio_format: str = DEFAULT_AUDIO_FORMAT,
                        tag_options: fix_music_tags.TagOptions = fix_music_tags.TagOptions(),
                        cookies_txt: str | None = None,
                        on_server: Callable[[str, str], bool] | None = None,
                        max_items: int = 0,
                        album_concurrency: int = 1,
                        fetch_lyrics: bool = False) -> Result:
    """Download an artist's whole discography (issue #32).

    Enumerates the artist's releases (`enumerate_artist`) and stages each through the ordinary
    album path (`run_download(mode="album", …)`) into ONE shared work dir — so the album
    download + tag logic (and its metadata parity) is reused verbatim. Then delivers the whole
    tree ONCE: a WebDAV upload mirrors `<Artist>/<Album>/…` into the library; a browser run ZIPs
    it under the artist name. `on_server` (dedup, issue #31) is passed through to every release so
    a re-run skips already-present tracks; `max_items` caps the number of releases (0 = unlimited).
    `album_concurrency` (>=2) downloads that many releases in parallel — each release is an
    independent yt-dlp run into its own `Artist/<album>/` folder, so parity is unaffected; results
    are aggregated in the calling thread. One failing release is logged and skipped — it does not
    abort the whole run.

    Every release is downloaded with `own_artist=<artist>` (issue #56), so tracks not credited to
    the artist — the compilation / "appears-on" / label uploads the `/releases` tab mixes in, whose
    broken metadata (label-as-artist) both defeats dedup and pollutes the library — are skipped up
    front. A release that is entirely such uploads simply contributes nothing (not an error).
    """
    work_base = _WORK_ROOT / job_id
    cookie_path = _write_cookie_file(job_id, cookies_txt)
    try:
        work_base.mkdir(parents=True, exist_ok=True)
        reporter.on_phase("metadata")
        artist, releases = enumerate_artist(
            url, cookiefile=str(cookie_path) if cookie_path else None, limit=max_items)
        if not releases:
            raise RuntimeError("Keine Releases gefunden — ist das eine YouTube-Music-Künstlerseite?")
        reporter.on_meta(artist, "")

        # Skip third-party compilation / "appears-on" / label uploads (issue #56): the
        # `/releases` tab mixes them in with the artist's OWN albums, but their artist tag is
        # the label (or absent — the performer only in the video title), so they never dedup
        # and would pollute the library with mis-tagged duplicates. Filter per-track by
        # crediting, always on for an artist run — but only with a CONFIDENT name (an
        # unresolved `_UNKNOWN_ARTIST` would drop everything). A "- Topic" channel suffix is
        # stripped so the match targets the bare performer name.
        own_artist = artist.removesuffix(" - Topic").strip() if artist else ""
        own_artist = own_artist if own_artist and own_artist != _UNKNOWN_ARTIST else None

        # Per-release reporter: forward track/meta to the outer reporter, but swallow phase
        # changes — the orchestrator owns the macro phase (so sub-albums don't flip it). With a
        # concurrent album pool the shared within-album track bar would flicker between releases,
        # so forward per-track progress only when albums run one at a time.
        forward_tracks = album_concurrency <= 1
        album_reporter = Reporter(on_phase=lambda phase: None,
                                  on_meta=reporter.on_meta,
                                  on_track=reporter.on_track if forward_tracks
                                  else (lambda cur, tot: None))

        total = len(releases)
        # Disambiguate release titles up front, single-threaded, so folder naming is deterministic
        # and independent of completion order: two releases can share a title (a reissue, a
        # re-released single) and would otherwise stage into the SAME `Artist/<title>/` folder,
        # clobbering each other's cover / re-tagging each other's tracks.
        used_titles: dict[str, int] = {}
        planned: list[tuple[int, dict, str]] = []
        for i, rel in enumerate(releases, 1):
            base = rel["title"]
            used_titles[base] = used_titles.get(base, 0) + 1
            album_name = base if used_titles[base] == 1 else f"{base} ({used_titles[base]})"
            planned.append((i, rel, album_name))

        delivered_all: list = []
        new_count = 0
        failed: list[str] = []
        done = 0
        reporter.on_phase("download")
        reporter.on_album(0, total, "")   # publish the album total before fan-out

        def _stage_release(i: int, rel: dict, album_name: str) -> Result:
            return run_download(job_id=f"{job_id}.{i}", url=rel["url"], genre=genre,
                                mode="album", destination=destination, reporter=album_reporter,
                                audio_format=audio_format, tag_options=tag_options,
                                cookies_txt=cookies_txt, on_server=on_server,
                                stage_dir=work_base, deliver=False, album_name=album_name,
                                own_artist=own_artist, fetch_lyrics=fetch_lyrics)

        # Fan out into up to `album_concurrency` parallel album downloads; results are aggregated
        # here in the calling thread (as_completed yields in this thread), so no locking is needed.
        workers = max(1, min(album_concurrency, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_stage_release, i, rel, name): name
                       for (i, rel, name) in planned}
            for fut in as_completed(futures):
                album_name = futures[fut]
                done += 1
                try:
                    sub = fut.result()
                    delivered_all += sub.delivered
                    new_count += sub.new_track_count
                except Exception:  # noqa: BLE001 - one bad release must not abort the whole run
                    log.exception("artist release failed: %s", album_name)
                    failed.append(album_name)
                reporter.on_album(done, total, album_name)

        # A failed release may have left incomplete-download artifacts behind (the shared
        # work dir is not cleaned per-release); never ship those partials.
        for tmp in (*work_base.rglob("*.part"), *work_base.rglob("*.ytdl")):
            tmp.unlink(missing_ok=True)

        # Cross-album track dedup (issue #56): drop a standalone single whose recording is also
        # inside a real (multi-track) album; two multi-track albums that share a title are left
        # alone (distinct recordings). Then drop the delivered entries whose file was removed so
        # the server index and the summary count match what actually ships.
        if own_artist:
            n_removed = _dedup_staged_tracks(work_base)
            if n_removed:
                delivered_all = [(a, t, rel) for (a, t, rel) in delivered_all
                                 if (work_base / rel).exists()]
                new_count = len(delivered_all)
                log.info("artist dedup: removed %d duplicate track(s) across albums", n_removed)

        # Nothing staged: dedup found everything already present, or every release failed.
        if not any(p.is_file() for p in work_base.rglob("*")):
            if failed and not delivered_all:
                raise RuntimeError(f"Keine Releases konnten geladen werden ({len(failed)} fehlgeschlagen).")
            return Result(summary="Keine neuen Titel", new_track_count=0)

        note = f" ({len(failed)} übersprungen)" if failed else ""
        if destination.type == "webdav":
            reporter.on_phase("upload")
            _upload_tree(destination, work_base)
            return Result(summary=f"WebDAV: {artist} — {new_count} Titel / {total} Releases{note}",
                          delivered=delivered_all, new_track_count=new_count)

        reporter.on_phase("packaging")
        # Releases stage under a single `<Artist>/` dir; zip that so the archive root is the
        # artist (not doubly nested). Fall back to the whole tree for the rare multi-artist case.
        dirs = [p for p in sorted(work_base.iterdir()) if p.is_dir()]
        stage_root = dirs[0] if len(dirs) == 1 else work_base
        zip_path = _WORK_ROOT / f"{job_id}.zip"
        _zip_dir(stage_root, zip_path, artist)
        return Result(summary=f"{artist}.zip{note}", zip_path=str(zip_path),
                      zip_name=f"{artist}.zip", delivered=delivered_all, new_track_count=new_count)
    finally:
        shutil.rmtree(work_base, ignore_errors=True)
        if cookie_path:
            cookie_path.unlink(missing_ok=True)
