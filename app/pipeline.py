"""Download pipeline — yt-dlp (as a library) + cover fetch + Navidrome tagging + WebDAV.

Metadata parity with the original bash scripts is guaranteed by building the
*identical* yt-dlp CLI flag list and converting it with `yt_dlp.parse_options()`
into the options dict that `YoutubeDL` consumes. We only add progress hooks on
top — the postprocessor/metadata behaviour is exactly what the CLI produced.
"""
from __future__ import annotations

import logging
import os
import shutil
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


def _fetch_cover(url: str, is_album: bool, dest: Path, cookiefile: str | None = None) -> Path | None:
    """Download the square album cover into `dest` (cover.jpg). Returns path or None."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _QuietLogger()}
    _apply_cookie_policy(opts, cookiefile)
    if is_album:
        opts["extract_flat"] = True  # playlist-level thumbnails (the album art)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        cover_url = pick_square_cover((data or {}).get("thumbnails"))
        if not cover_url:
            return None
        resp = httpx.get(cover_url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest
    except Exception as exc:  # cover is best-effort; embedded thumbnail remains
        log.warning("cover fetch failed: %s", exc)
        return None


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
                 cookies_txt: str | None = None) -> Result:
    """Execute one download end-to-end and return a Result.

    Both destinations stage into a temp work dir; then either a ZIP is packaged
    (browser) or the tree is uploaded (webdav). Raises on fatal errors.

    `tag_options` gates which metadata fields are written (issue #7); the default
    (all on) keeps the output byte-identical to the original tool.

    `cookies_txt` is the user's decrypted Netscape cookies.txt (issue #9); when
    given it is handed to every yt-dlp call so bot-checks/age gates don't block
    the download. When omitted, no `cookiefile` is set — the output stays
    byte-identical (metadata parity).
    """
    is_album = mode != "single"

    # Materialise the cookie to a 0600 file (kept outside work_base so the WebDAV
    # delivery never ships it); cleaned up in `finally`. None when no cookie → the
    # no-cookie path is byte-identical (metadata parity).
    cookie_path: Path | None = None
    work_base = _WORK_ROOT / job_id
    try:
        cookie_path = _write_cookie_file(job_id, cookies_txt)
        cookiefile = str(cookie_path) if cookie_path else None

        # 1) Metadata → primary artist + album, to build the output directory.
        reporter.on_phase("metadata")
        artist_raw, album_raw = _probe_meta(url, is_album, cookiefile=cookiefile)
        primary_artist = _primary_artist(artist_raw)
        album = (album_raw or "Unbekannt Album") if is_album else "Singles"
        reporter.on_meta(primary_artist, album)

        # 2) Always stage into a temp work dir; the delivery step then packages a
        #    ZIP (browser) or uploads the tree (webdav). The work dir is removed in
        #    `finally` so failed jobs don't leak it; the browser ZIP lives outside it.
        work_base.mkdir(parents=True, exist_ok=True)

        # `primary_artist` is interpolated literally (not a yt-dlp `%(...)s` field),
        # so sanitise it ourselves to keep it a single, traversal-safe path segment.
        subfolder = "%(album)s" if is_album else "Singles"
        out_tmpl = str(work_base / _safe_segment(primary_artist) / subfolder / "%(title)s.%(ext)s")

        # 3) Download (parity-safe opts from parse_options + our hooks).
        flags = _apply_audio_format(_ALBUM_FLAGS if is_album else _SINGLE_FLAGS, audio_format)
        flags = _apply_tag_options(flags, tag_options)
        if tag_options.genre:
            flags += ["--postprocessor-args", f"ffmpeg:-metadata genre={genre}"]
        flags += ["-o", out_tmpl]
        opts = _build_ydl_opts(flags)
        opts.update({"quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger()})
        _apply_cookie_policy(opts, cookiefile)

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
            raise RuntimeError("Download lieferte keine Dateien (siehe Logs).")
        album_dir = Path(finished_dirs.most_common(1)[0][0])
        if not album_dir.is_dir():
            # Defensive: keeps fix_music_tags' sys.exit path (BaseException) unreachable.
            raise RuntimeError(f"Album-Verzeichnis fehlt: {album_dir}")

        # 4) Square cover (skipped when the cover field is toggled off).
        cover_path = (_fetch_cover(url, is_album, album_dir / "cover.jpg", cookiefile=cookiefile)
                      if tag_options.cover else None)

        # 5) Navidrome tag correction (unchanged logic from fix_music_tags.py),
        #    gated per tag_options — all-on keeps the original behaviour.
        reporter.on_phase("tags")
        fix_music_tags.process_directory(
            str(album_dir),
            str(cover_path) if cover_path else None,
            album,
            primary_artist,
            tag_options,
        )

        # 6) Deliver.
        if destination.type == "webdav":
            reporter.on_phase("upload")
            _upload_tree(destination, work_base)
            return Result(summary=f"WebDAV: {primary_artist}/{album}")

        # browser → package the tagged album folder as a ZIP for download
        reporter.on_phase("packaging")
        root_name = f"{primary_artist} - {album}"
        zip_path = _WORK_ROOT / f"{job_id}.zip"
        _zip_dir(album_dir, zip_path, root_name)
        return Result(summary=f"{root_name}.zip", zip_path=str(zip_path), zip_name=f"{root_name}.zip")
    finally:
        shutil.rmtree(work_base, ignore_errors=True)
        if cookie_path:
            cookie_path.unlink(missing_ok=True)
