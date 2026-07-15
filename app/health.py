"""Library health check & repair (roadmap 05).

An audit of the WebDAV library that finds metadata/file problems and fixes the fixable ones with
machinery Soundpull already has — keeping older downloads up to the app's current standards. NO
online metadata lookups (that would fight the parity philosophy); fixes only where a *correct*
value is derivable (existing sidecar machinery, in-folder cover, earliest year, the user's default
genre).

Two tiers:
- **cheap** — one directory walk (`library_index.iter_library_dirs`), no downloads: H1 missing
  `.lrc`, H2 stray thumbnails/fragments, H3 empty folders, H4 non-audio junk.
- **deep** — per album, bounded: download the album to a staging dir, read tags with mutagen,
  evaluate H5 album-split-by-year, H6 missing cover, H7 missing genre, H8 missing album/album-artist,
  H9 corrupt audio (ffmpeg decode); fix the fixable ones and re-upload only changed files.

`fix_music_tags.py` is **frozen** — every tag read/rewrite here goes through mutagen directly (or
reuses only the neutral helpers `fix_music_tags._vorbis_cover`, `fix_music_tags._SUPPORTED_EXTS`,
and `pipeline._save_easy_tags` which keeps MP3 on ID3v2.3). This module never touches the pipeline
flag lists or the frozen normalization rules, so metadata parity holds by construction.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlmodel import select

from app import fix_music_tags, library_index, library_ops, webdav_util

log = logging.getLogger("health")

# --- Check identifiers ------------------------------------------------------

CHEAP_CHECKS = ("lyrics_missing", "stray_file", "empty_folder", "junk_file")
DEEP_CHECKS = ("year_split", "cover_missing", "genre_missing", "album_tag_missing", "corrupt_audio")
# Checks with an automated fix (the rest are report-only — the user acts elsewhere).
FIXABLE = {"lyrics_missing", "stray_file", "empty_folder", "year_split", "cover_missing",
           "genre_missing"}

_IMG_EXTS = (".jpg", ".jpeg", ".webp", ".png")
_FRAGMENT_EXTS = (".part", ".ytdl")
_ALLOWED_NON_AUDIO = (".lrc", ".m3u8", ".m3u")


@dataclass
class Finding:
    check_id: str
    rel_path: str          # file OR folder (H3/H5), relative to webdav_folder
    detail: str = ""       # human hint (filename, error line, "2019 vs 2021", …)
    fixable: bool = False


@dataclass
class Report:
    created_at: str
    cheap: list[Finding] = field(default_factory=list)
    deep: list[Finding] = field(default_factory=list)
    checked_albums: list[str] = field(default_factory=list)   # deep-check resumability
    cheap_run_at: Optional[str] = None


@dataclass
class FixResult:
    ok: bool = False
    fixed_paths: list[str] = field(default_factory=list)   # rels to prune from the report
    error: Optional[str] = None


# --- Background run registry (mirrors app.duplicates) -----------------------

@dataclass
class HealthState:
    phase: str = "queued"       # queued | scanning | checking | done | error (i18n keys on page)
    error: Optional[str] = None
    checked_count: int = 0      # albums deep-checked so far (deep modes)
    total_count: int = 0        # albums to deep-check this run
    finding_count: int = 0
    finished: bool = False


_health: dict[int, HealthState] = {}
_health_lock = threading.Lock()
_health_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="health")


def get_health_state(user_id: int) -> Optional[HealthState]:
    with _health_lock:
        return _health.get(user_id)


def is_health_running(user_id: int) -> bool:
    with _health_lock:
        st = _health.get(user_id)
        return st is not None and not st.finished


def start_health(user_id: int, mode: str, *, album_prefix: Optional[str] = None,
                 limit: int = 25) -> bool:
    """Kick off a background health run. `mode` ∈ {cheap, deep_batch, deep_album}. False if busy."""
    with _health_lock:
        st = _health.get(user_id)
        if st is not None and not st.finished:
            return False
        _health[user_id] = HealthState(phase="queued")

    def _set(**kw) -> None:
        with _health_lock:
            st = _health.get(user_id)
            if st is not None:
                for k, v in kw.items():
                    setattr(st, k, v)

    def _run() -> None:
        try:
            if mode == "cheap":
                _set(phase="scanning")
                report = run_cheap_checks(user_id)
            elif mode == "deep_album" and album_prefix:
                _set(phase="checking", total_count=1)
                report = deep_check_album(user_id, album_prefix,
                                          progress=lambda d, t: _set(checked_count=d, total_count=t))
            else:  # deep_batch
                _set(phase="checking")
                report = deep_check_batch(
                    user_id, limit=limit,
                    progress=lambda d, t: _set(phase="checking", checked_count=d, total_count=t))
            _set(phase="done", finished=True,
                 finding_count=len(report.cheap) + len(report.deep))
        except Exception as exc:  # noqa: BLE001 - a failed run must not kill the worker
            log.exception("health run (%s) for user %s failed", mode, user_id)
            _set(phase="error", finished=True, error=str(exc))

    _health_executor.submit(_run)
    return True


# --- Client / settings loading ---------------------------------------------

def _load(user_id: int):
    """Return ``(client, base, us)`` for the user, or raise if no WebDAV target (like scan_webdav)."""
    from app.db import session_scope
    from app.models import UserSettings
    from app.security import decrypt_secret

    with session_scope() as session:
        us = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        if not us or not us.webdav_url:
            raise ValueError("Kein WebDAV-Ziel im Profil hinterlegt.")
        url, username = us.webdav_url, us.webdav_username
        password = decrypt_secret(us.webdav_password_enc) if us.webdav_password_enc else None
        base = (us.webdav_folder or "").strip("/")
        # Detach the scalar settings we need so we can use them outside the session.
        snapshot = {"default_genre": us.default_genre,
                    "fetch_synced_lyrics": bool(us.fetch_synced_lyrics)}
    client = webdav_util.make_client(url, username, password)
    return client, base, snapshot


def _join(base: str, rel: str) -> str:
    return f"{base}/{rel}" if base else rel


# --- Cheap checks (pure over directory tuples) ------------------------------

def _is_cover_name(name: str) -> bool:
    return os.path.splitext(name)[0].lower() == "cover"


def detect_cheap(dir_entries, *, lyrics_enabled: bool) -> list[Finding]:
    """H1–H4 over ``(dir_rel, subdirs, files)`` tuples — pure, so it is unit-testable offline."""
    findings: list[Finding] = []
    for dir_rel, subdirs, files in dir_entries:
        # H3: an empty folder (no files AND no sub-directories). The base itself is never flagged.
        if dir_rel and not files and not subdirs:
            findings.append(Finding("empty_folder", dir_rel, detail=posixpath.basename(dir_rel),
                                    fixable=True))
            continue
        lower = {f.lower() for f in files}
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = f"{dir_rel}/{f}" if dir_rel else f
            if ext in fix_music_tags._SUPPORTED_EXTS:
                # H1: audio file with no sibling <stem>.lrc.
                if lyrics_enabled and (os.path.splitext(f)[0] + ".lrc").lower() not in lower:
                    findings.append(Finding("lyrics_missing", full, detail=f, fixable=True))
            elif ext in _ALLOWED_NON_AUDIO:
                continue
            elif ext in _IMG_EXTS:
                if not _is_cover_name(f):  # H2: a stray (non-cover) thumbnail
                    findings.append(Finding("stray_file", full, detail=f, fixable=True))
            elif ext in _FRAGMENT_EXTS:   # H2: an interrupted-download fragment
                findings.append(Finding("stray_file", full, detail=f, fixable=True))
            else:                          # H4: unknown non-audio junk — report only
                findings.append(Finding("junk_file", full, detail=f, fixable=False))
    return findings


def run_cheap_checks(user_id: int, progress: Optional[Callable[[str], None]] = None) -> Report:
    """Walk the library once and build the cheap (H1–H4) findings, merged into the report."""
    if progress:
        progress("scanning")
    client, base, us = _load(user_id)
    errors: list = []
    entries = list(library_index.iter_library_dirs(client, base, errors=errors))
    if errors:
        log.warning("health cheap scan: %d listing(s) failed — report may be partial", len(errors))
    findings = detect_cheap(entries, lyrics_enabled=us["fetch_synced_lyrics"])

    report = load_report(user_id) or Report(created_at=_now_iso())
    report.cheap = findings
    report.cheap_run_at = _now_iso()
    _persist(user_id, report)
    return report


# --- Album enumeration + staging (deep checks) ------------------------------

def _list_albums(client, base: str) -> list[str]:
    """Sorted album folders = every directory that DIRECTLY holds ≥1 audio file (roadmap 05)."""
    albums: list[str] = []
    for dir_rel, _subdirs, files in library_index.iter_library_dirs(client, base):
        if dir_rel and any(os.path.splitext(f)[1].lower() in fix_music_tags._SUPPORTED_EXTS
                           for f in files):
            albums.append(dir_rel)
    return sorted(albums)


def _album_audio_rels(client, base: str, album_prefix: str) -> list[str]:
    """Library-relative paths of the audio files directly inside `album_prefix`."""
    prefix = f"{base}/" if base else ""
    try:
        entries = client.ls(_join(base, album_prefix), detail=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("health: listing album %r failed: %s", album_prefix, exc)
        return []
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") == "directory":
            continue
        name = str(entry.get("name", "")).rstrip("/")
        if not name or not name.lower().endswith(fix_music_tags._SUPPORTED_EXTS):
            continue
        rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
        out.append(rel)
    return out


def _staging_dir() -> Path:
    from app.pipeline import _WORK_ROOT

    root = _WORK_ROOT / "health"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=root))


def _download_album(client, base: str, album_rels: list[str], work: Path) -> list[tuple[str, Path]]:
    """Download each album track into `work`; return [(rel, local_path)] for the ones fetched."""
    staged: list[tuple[str, Path]] = []
    for rel in album_rels:
        local = work / posixpath.basename(rel)
        try:
            webdav_util.download_file(client, _join(base, rel), local)
            staged.append((rel, local))
        except Exception as exc:  # noqa: BLE001 - one bad download must not abort the album
            log.warning("health: downloading %r failed: %s", rel, exc)
    return staged


# --- Tag / cover / integrity helpers (mutagen-level, fix_music_tags-free) ---

def _open_easy(path: Path):
    from mutagen import File as MutagenFile

    try:
        return MutagenFile(str(path), easy=True)
    except Exception:  # noqa: BLE001 - unreadable tags
        return None


def _easy_get(mf, key: str) -> str:
    try:
        val = mf.get(key) if mf is not None else None
    except Exception:  # noqa: BLE001
        return ""
    return (str(val[0]).strip() if val else "")


def earliest_date(dates) -> Optional[str]:
    """Earliest non-empty date string (YYYY / YYYY-MM-DD / YYYYMMDD all sort chronologically).

    Mirrors the rule of `pipeline._unify_album_year` (``min(dates)``), extracted as a pure helper
    so H5 can decide AND report which files must change (for selective re-upload)."""
    vals = [str(d).strip() for d in dates if d and str(d).strip()]
    return min(vals) if vals else None


def _has_cover(path: Path) -> bool:
    """Whether the file already carries embedded front-cover art (per-format)."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                return bool(ID3(str(path)).getall("APIC"))
            except ID3NoHeaderError:
                return False
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            a = MP4(str(path))
            return bool(a.tags and "covr" in a.tags)
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
        opener = OggOpus if ext == ".opus" else OggVorbis
        return "metadata_block_picture" in opener(str(path))
    except Exception:  # noqa: BLE001 - unreadable → assume present so we never falsely "fix"
        return True


def _embed_cover(path: Path, jpeg: bytes) -> None:
    """Embed `jpeg` as front cover, mirroring the exact `fix_music_tags` per-format conventions."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags["APIC:"] = APIC(encoding=0, mime="image/jpeg", type=3, desc="Cover", data=jpeg)
        tags.save(str(path), v2_version=3)
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4Cover
        a = MP4(str(path))
        a["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        a.save()
    else:
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
        opener = OggOpus if ext == ".opus" else OggVorbis
        a = opener(str(path))
        a["metadata_block_picture"] = [fix_music_tags._vorbis_cover(jpeg)]
        a.pop("coverart", None)
        a.save()


def _decode_error(path: Path) -> Optional[str]:
    """Run an ffmpeg decode-only pass; return the first error line if the file is corrupt, else None."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None  # can't verify without ffmpeg → don't flag
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        log.warning("health: ffmpeg decode check failed to run on %r: %s", path.name, exc)
        return None
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or stderr:
        return (stderr.splitlines()[0] if stderr else f"ffmpeg exit {proc.returncode}")
    return None


# --- Deep checks (download → inspect) ---------------------------------------

def _detect_album(album_prefix: str, staged: list[tuple[str, Path]]) -> list[Finding]:
    """Evaluate H5–H9 on the already-downloaded copies of one album."""
    findings: list[Finding] = []
    dates: list[str] = []
    for rel, local in staged:
        mf = _open_easy(local)
        date = _easy_get(mf, "date")
        if date:
            dates.append(date)
        if not _has_cover(local):
            findings.append(Finding("cover_missing", rel, detail=posixpath.basename(rel),
                                    fixable=True))
        if not _easy_get(mf, "genre"):
            findings.append(Finding("genre_missing", rel, detail=posixpath.basename(rel),
                                    fixable=True))
        if not _easy_get(mf, "album") or not _easy_get(mf, "albumartist"):
            findings.append(Finding("album_tag_missing", rel, detail=posixpath.basename(rel),
                                    fixable=False))
        err = _decode_error(local)
        if err:
            findings.append(Finding("corrupt_audio", rel, detail=err, fixable=False))
    # H5: one album folder whose tracks carry ≥2 distinct years → Navidrome splits it.
    if len({d for d in dates}) >= 2:
        findings.append(Finding("year_split", album_prefix,
                                detail=f"{min(dates)} … {max(dates)}", fixable=True))
    return findings


def deep_check_album(user_id: int, album_prefix: str,
                     progress: Optional[Callable[[int, int], None]] = None) -> Report:
    """Deep-check a single album: download, evaluate H5–H9, store its findings, clean up staging."""
    client, base, _ = _load(user_id)
    if progress:
        progress(0, 1)
    work = _staging_dir()
    try:
        staged = _download_album(client, base, _album_audio_rels(client, base, album_prefix), work)
        findings = _detect_album(album_prefix, staged)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report = load_report(user_id) or Report(created_at=_now_iso())
    # Replace any prior findings for this album, and mark it checked (resumability).
    report.deep = [f for f in report.deep if not _under(f, album_prefix)] + findings
    if album_prefix not in report.checked_albums:
        report.checked_albums.append(album_prefix)
    _persist(user_id, report)
    if progress:
        progress(1, 1)
    return report


def deep_check_batch(user_id: int, *, limit: int = 25,
                     progress: Optional[Callable[[int, int], None]] = None) -> Report:
    """Deep-check up to `limit` not-yet-checked albums, appending to the report (resumable)."""
    client, base, _ = _load(user_id)
    report = load_report(user_id) or Report(created_at=_now_iso())
    checked = set(report.checked_albums)
    todo = [a for a in _list_albums(client, base) if a not in checked][:limit]
    total = len(todo)
    if progress:
        progress(0, total)
    for i, album in enumerate(todo, start=1):
        work = _staging_dir()
        try:
            staged = _download_album(client, base, _album_audio_rels(client, base, album), work)
            findings = _detect_album(album, staged)
        except Exception as exc:  # noqa: BLE001 - one album must not abort the batch
            log.warning("health: deep-check of %r failed: %s", album, exc)
            findings = []
        finally:
            shutil.rmtree(work, ignore_errors=True)
        report.deep = [f for f in report.deep if not _under(f, album)] + findings
        if album not in report.checked_albums:
            report.checked_albums.append(album)
        _persist(user_id, report)   # incremental → progress survives a crash mid-batch
        if progress:
            progress(i, total)
    return report


def _under(finding: Finding, album_prefix: str) -> bool:
    """True if a deep finding belongs to `album_prefix` (the album folder itself or a file in it)."""
    pref = album_prefix.rstrip("/")
    return finding.rel_path == pref or finding.rel_path.startswith(pref + "/")


# --- Fixes ------------------------------------------------------------------

def fix_finding(user_id: int, check_id: str, rel_path: str) -> FixResult:
    """Apply a CHEAP fix (H1 lyrics / H2 stray / H3 empty folder). Best-effort."""
    try:
        if check_id == "lyrics_missing":
            folder = posixpath.dirname(rel_path)
            written, _skipped, _missing, _errors = library_index.backfill_lyrics(
                user_id, prefix=folder or None)
            # The whole folder was backfilled → the page prunes all lyrics findings under it.
            return FixResult(ok=written > 0, fixed_paths=[rel_path])
        if check_id == "stray_file":
            library_ops.trash_track(user_id, rel_path)
            return FixResult(ok=True, fixed_paths=[rel_path])
        if check_id == "empty_folder":
            _trash_empty_folder(user_id, rel_path)
            return FixResult(ok=True, fixed_paths=[rel_path])
        return FixResult(ok=False, error=f"not fixable: {check_id}")
    except Exception as exc:  # noqa: BLE001 - surface the error to the caller, never crash
        log.warning("health fix %s on %r failed: %s", check_id, rel_path, exc)
        return FixResult(ok=False, error=str(exc))


def _trash_empty_folder(user_id: int, folder_rel: str) -> None:
    """Remove an empty folder — via the index-aware trash when possible, else a raw delete."""
    try:
        library_ops.trash_folder(user_id, folder_rel)
    except Exception:  # noqa: BLE001 - an empty folder has no index rows; fall back to a raw delete
        client, base, _ = _load(user_id)
        webdav_util.delete_path(client, _join(base, webdav_util.resolve_rel(folder_rel)))


def fix_album(user_id: int, album_prefix: str, fix_ids: set[str]) -> FixResult:
    """Apply the requested DEEP fixes (H5 year / H6 cover / H7 genre) to one album.

    Downloads the album once, applies only the fixes in `fix_ids`, re-uploads ONLY the changed
    files, and returns the rel_paths that were repaired (so the page prunes them). Best-effort.
    """
    try:
        client, base, us = _load(user_id)
    except Exception as exc:  # noqa: BLE001
        return FixResult(ok=False, error=str(exc))
    work = _staging_dir()
    fixed: list[str] = []
    try:
        album_rels = _album_audio_rels(client, base, album_prefix)
        staged = _download_album(client, base, album_rels, work)
        changed: set[Path] = set()

        if "year_split" in fix_ids:
            changed |= _fix_year(staged)
        if "cover_missing" in fix_ids:
            changed |= _fix_cover(client, base, album_prefix, work, staged)
        if "genre_missing" in fix_ids:
            changed |= _fix_genre(staged, us["default_genre"])

        from app.pipeline import _upload_with_retry
        for rel, local in staged:
            if local in changed:
                try:
                    _upload_with_retry(client, str(local), _join(base, rel))
                    fixed.append(rel)
                except Exception as exc:  # noqa: BLE001 - one bad upload must not abort the rest
                    log.warning("health: re-uploading %r failed: %s", rel, exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # The album folder itself is the H5 finding's rel_path; report it fixed when any file changed.
    if "year_split" in fix_ids and fixed:
        fixed.append(album_prefix)
    return FixResult(ok=bool(fixed), fixed_paths=fixed)


def _fix_year(staged: list[tuple[str, Path]]) -> set[Path]:
    """H5: force every track's date to the earliest (mirrors `pipeline._unify_album_year`)."""
    from app.pipeline import _save_easy_tags

    dates = []
    loaded: list[tuple[Path, object]] = []
    for _rel, local in staged:
        mf = _open_easy(local)
        if mf is None:
            continue
        loaded.append((local, mf))
        d = _easy_get(mf, "date")
        if d:
            dates.append(d)
    earliest = earliest_date(dates)
    if earliest is None or len({d for d in dates}) < 2:
        return set()
    changed: set[Path] = set()
    for local, mf in loaded:
        if _easy_get(mf, "date") != earliest:
            try:
                mf["date"] = [earliest]
                _save_easy_tags(mf)
                changed.add(local)
            except Exception as exc:  # noqa: BLE001
                log.warning("health: unifying year on %r failed: %s", local.name, exc)
    return changed


def _fix_cover(client, base: str, album_prefix: str, work: Path,
               staged: list[tuple[str, Path]]) -> set[Path]:
    """H6: embed a cover into tracks lacking one, sourced from `cover.jpg` or the largest in-album art."""
    jpeg = _album_cover_bytes(client, base, album_prefix, work, staged)
    if not jpeg:
        return set()
    return _fix_cover_local(staged, jpeg)


def _fix_cover_local(staged: list[tuple[str, Path]], jpeg: bytes) -> set[Path]:
    """Embed `jpeg` into every staged track that lacks a cover; return the changed paths."""
    changed: set[Path] = set()
    for _rel, local in staged:
        if _has_cover(local):
            continue
        try:
            _embed_cover(local, jpeg)
            changed.add(local)
        except Exception as exc:  # noqa: BLE001
            log.warning("health: embedding cover into %r failed: %s", local.name, exc)
    return changed


def _album_cover_bytes(client, base: str, album_prefix: str, work: Path,
                       staged: list[tuple[str, Path]]) -> Optional[bytes]:
    """Cover source for H6: a `cover.jpg` in the album folder, else the largest embedded art found."""
    # 1) cover.jpg / cover.jpeg / cover.png sitting in the folder.
    for name in ("cover.jpg", "cover.jpeg", "cover.png"):
        remote = _join(base, f"{album_prefix}/{name}")
        try:
            if webdav_util.path_exists(client, remote):
                local = work / name
                webdav_util.download_file(client, remote, local)
                data = local.read_bytes()
                if data:
                    return data
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    # 2) The largest embedded cover already present on a sibling track.
    best: Optional[bytes] = None
    for _rel, local in staged:
        data = _embedded_cover_bytes(local)
        if data and (best is None or len(data) > len(best)):
            best = data
    return best


def _embedded_cover_bytes(path: Path) -> Optional[bytes]:
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                apics = ID3(str(path)).getall("APIC")
            except ID3NoHeaderError:
                return None
            return apics[0].data if apics else None
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            tags = MP4(str(path)).tags
            covr = tags.get("covr") if tags else None
            return bytes(covr[0]) if covr else None
        import base64
        from mutagen.flac import Picture
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
        opener = OggOpus if ext == ".opus" else OggVorbis
        blocks = opener(str(path)).get("metadata_block_picture")
        if not blocks:
            return None
        return Picture(base64.b64decode(blocks[0])).data
    except Exception:  # noqa: BLE001
        return None


def _fix_genre(staged: list[tuple[str, Path]], default_genre: str) -> set[Path]:
    """H7: write `default_genre` to tracks that have no genre tag."""
    from app.pipeline import _save_easy_tags

    changed: set[Path] = set()
    for _rel, local in staged:
        mf = _open_easy(local)
        if mf is None or _easy_get(mf, "genre"):
            continue
        try:
            mf["genre"] = [default_genre]
            _save_easy_tags(mf)
            changed.add(local)
        except Exception as exc:  # noqa: BLE001
            log.warning("health: writing genre on %r failed: %s", local.name, exc)
    return changed


# --- Persistence (mirrors app.duplicates) -----------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(report: Report) -> str:
    return json.dumps({
        "cheap": [asdict(f) for f in report.cheap],
        "deep": [asdict(f) for f in report.deep],
        "checked_albums": report.checked_albums,
        "cheap_run_at": report.cheap_run_at,
    })


def _persist(user_id: int, report: Report) -> None:
    from app.db import session_scope
    from app.models import HealthReport

    with session_scope() as session:
        row = session.exec(select(HealthReport).where(HealthReport.user_id == user_id)).first()
        if row is None:
            row = HealthReport(user_id=user_id)
        row.findings = _payload(report)
        row.created_at = datetime.now(timezone.utc)
        session.add(row)


def save_report(user_id: int, report: Report) -> None:
    """Persist an UPDATED report (after a fix prunes findings) WITHOUT re-stamping created_at."""
    from app.db import session_scope
    from app.models import HealthReport

    with session_scope() as session:
        row = session.exec(select(HealthReport).where(HealthReport.user_id == user_id)).first()
        if row is None:
            return
        row.findings = _payload(report)
        session.add(row)


def load_report(user_id: int) -> Optional[Report]:
    from app.db import session_scope
    from app.models import HealthReport

    with session_scope() as session:
        row = session.exec(select(HealthReport).where(HealthReport.user_id == user_id)).first()
        if row is None:
            return None
        data = json.loads(row.findings or "{}")
        if not isinstance(data, dict):
            data = {}
        created = row.created_at
    created_iso = created.isoformat() if isinstance(created, datetime) else str(created)

    def _findings(items) -> list[Finding]:
        return [Finding(check_id=d["check_id"], rel_path=d["rel_path"],
                        detail=d.get("detail", ""), fixable=d.get("fixable", False))
                for d in (items or [])]

    return Report(created_at=created_iso, cheap=_findings(data.get("cheap")),
                  deep=_findings(data.get("deep")), checked_albums=data.get("checked_albums", []),
                  cheap_run_at=data.get("cheap_run_at"))
