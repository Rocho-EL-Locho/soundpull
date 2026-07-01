"""Download pipeline — yt-dlp (as a library) + cover fetch + Navidrome tagging + WebDAV.

Metadata parity with the original bash scripts is guaranteed by building the
*identical* yt-dlp CLI flag list and converting it with `yt_dlp.parse_options()`
into the options dict that `YoutubeDL` consumes. We only add progress hooks on
top — the postprocessor/metadata behaviour is exactly what the CLI produced.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
import yt_dlp

from app import fix_music_tags
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


def _safe_segment(name: str) -> str:
    """Make a metadata string safe as a single path segment (no traversal)."""
    seg = name.replace("/", "_").replace("\\", "_").replace("\x00", "").strip()
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
    # (artist, title) of every track uploaded to WebDAV — fed to the server index
    # (issue #21). Populated for all modes; the caller records it only for WebDAV.
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


def _probe_playlist(url: str, cookiefile: str | None = None) -> tuple[str, str, int]:
    """Read a playlist's title, uploader and entry count (issue #11).

    Uses `extract_flat` so we only touch the playlist envelope, not every video —
    fast, and enough to name the download and seed the progress total. A playlist
    spans many artists/albums, so (unlike `_probe_meta`) there is no single
    artist/album to collapse to; each track is tagged from its own metadata later.
    Returns (title, uploader, count); count is 0 when unknown.
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
    return title, uploader, int(count or 0)


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


def _make_match_filter(on_server: Callable[[str, str], bool]):
    """yt-dlp match_filter: reject a track already on the server (issue #21).

    yt-dlp extracts each entry's full metadata (artist/track) BEFORE downloading the
    media, then calls this; returning a string skips the entry. `incomplete` is True
    during playlist enumeration when tags aren't final — we defer (return None) so the
    real check runs on the complete info_dict.
    """
    def _filter(info_dict: dict, incomplete: bool = False) -> str | None:
        if incomplete or info_dict.get("_type") in ("playlist", "multi_video"):
            return None
        title = info_dict.get("track") or info_dict.get("title") or ""
        artists = info_dict.get("artists")
        first = artists[0] if isinstance(artists, list) and artists else ""
        artist = info_dict.get("artist") or first or info_dict.get("uploader") or ""
        if title and on_server(artist, title):
            return f"schon auf dem Server: {artist} - {title}".strip()
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


def _upload_tree(dest: Destination, local_root: Path) -> None:
    from app.webdav_util import make_client

    client = make_client(dest.webdav_url, dest.webdav_username, dest.webdav_password)
    prefix = (dest.webdav_folder or "").strip("/")
    for path in sorted(local_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        remote = f"{prefix}/{rel}" if prefix else rel
        parent = "/".join(remote.split("/")[:-1])
        if parent:
            _ensure_remote_dir(client, parent)
        client.upload_file(str(path), remote, overwrite=True)


def run_download(*, job_id: str, url: str, genre: str, mode: str,
                 destination: Destination, reporter: Reporter,
                 audio_format: str = DEFAULT_AUDIO_FORMAT,
                 tag_options: fix_music_tags.TagOptions = fix_music_tags.TagOptions(),
                 cookies_txt: str | None = None,
                 on_server: Callable[[str, str], bool] | None = None,
                 existing_tracks: list[dict] | None = None) -> Result:
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

    `on_server(artist, title) -> bool` turns this into a playlist SYNC (issue #21):
    it becomes a yt-dlp match_filter that skips tracks already on the server, so only
    new ones download (zero new tracks is then a normal, non-error outcome). It is
    parity-safe — the filter only selects which entries download, never how the
    downloaded ones are tagged. When None (every non-sync call) the behaviour and
    output are unchanged. `existing_tracks` is the subscription's prior m3u manifest,
    merged with the new tracks to rebuild the COMPLETE `<name>.m3u8`.
    """
    is_playlist = mode == "playlist"
    is_single = mode == "single"
    is_album = not is_playlist and not is_single  # unknown/legacy modes → album
    is_sync = is_playlist and on_server is not None  # issue #21: only-new-tracks sync

    # Materialise the cookie to a 0600 file (kept outside work_base so the WebDAV
    # delivery never ships it); cleaned up in `finally`. None when no cookie → the
    # no-cookie path is byte-identical (metadata parity).
    cookie_path: Path | None = None
    work_base = _WORK_ROOT / job_id
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
            pl_title, pl_uploader, pl_count = _probe_playlist(url, cookiefile=cookiefile)
            if settings.max_playlist_items > 0 and pl_count:
                pl_count = min(pl_count, settings.max_playlist_items)
            reporter.on_meta(pl_uploader, pl_title)
            if pl_count:
                reporter.on_track(0, pl_count)
            # A real playlist: ALL tracks in one folder (the playlist name), plus an
            # .m3u8 written after tagging. `pl_title` is interpolated literally, so
            # sanitise it into one safe path segment; the track filename is a yt-dlp
            # template (its fields are sanitised by yt-dlp).
            playlist_dir = work_base / _safe_segment(pl_title)
            out_tmpl = str(playlist_dir / _PLAYLIST_TRACK_TMPL)
            base_flags = _SINGLE_FLAGS  # per-track tags, no album track-number remap
        else:
            artist_raw, album_raw = _probe_meta(url, is_album, cookiefile=cookiefile)
            primary_artist = _primary_artist(artist_raw)
            album = (album_raw or "Unbekannt Album") if is_album else "Singles"
            reporter.on_meta(primary_artist, album)
            # `primary_artist` is interpolated literally (not a yt-dlp `%(...)s`
            # field), so sanitise it ourselves to keep it a single, traversal-safe
            # path segment.
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
        if on_server is not None:  # sync: skip tracks already on the server (issue #21)
            opts["match_filter"] = _make_match_filter(on_server)

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

        if not finished_dirs:
            if is_sync:
                # Nothing new on the playlist — a normal sync outcome, not an error.
                # Return the prior manifest unchanged so the subscription keeps it.
                return Result(summary="Keine neuen Titel", new_track_count=0,
                              playlist_files=list(existing_tracks or []),
                              playlist_name=pl_title)
            raise RuntimeError("Download lieferte keine Dateien (siehe Logs).")

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
            # Record what we're delivering (issue #21): (artist, title) per track,
            # read from the FINAL tags so the server index matches later lookups.
            new_entries = _m3u_entries_from_paths(tracks)
            delivered = [(e["artist"], e["title"]) for e in new_entries]
            if is_sync:
                # Incremental sync: rebuild the COMPLETE playlist from prior + new
                # tracks so Navidrome shows every track, though we upload only the new
                # files (+ the regenerated m3u8). Prior tracks already sit in the folder.
                manifest = _merge_manifest(existing_tracks, new_entries)
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
            cover_path = (_fetch_cover(url, is_album, album_dir / "cover.jpg", cookiefile=cookiefile)
                          if tag_options.cover else None)
            fix_music_tags.process_directory(
                str(album_dir),
                str(cover_path) if cover_path else None,
                album,
                primary_artist,
                tag_options,
            )
            # Record delivered tracks for the server index (issue #21). Album/single
            # force one primary artist, so pair each track's title with it.
            audio_files = sorted(p for p in album_dir.glob("*")
                                 if p.suffix.lower() in fix_music_tags._SUPPORTED_EXTS)
            delivered = [(primary_artist, _track_meta(p)[0]) for p in audio_files]
            manifest = []
            stage_root = album_dir
            root_name = f"{primary_artist} - {album}"
            webdav_label = f"{primary_artist}/{album}"

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
        shutil.rmtree(work_base, ignore_errors=True)
        if cookie_path:
            cookie_path.unlink(missing_ok=True)
