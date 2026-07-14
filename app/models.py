"""SQLModel tables: User, UserSettings, DownloadHistory.

Note: deliberately NO `from __future__ import annotations` here — SQLModel
resolves quoted relationship forward refs (e.g. "UserSettings") via its own
registry, and the future import would double-stringify them and break mapping.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from app.genres import DEFAULT_GENRE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    sub: str = Field(index=True, unique=True)  # OIDC subject — stable identity key
    email: str | None = None
    username: str | None = None
    display_name: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    last_login_at: datetime = Field(default_factory=_utcnow)

    settings: Optional["UserSettings"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )


class UserSettings(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    default_genre: str = DEFAULT_GENRE
    default_mode: str = "album"  # album | single | playlist | artist
    # Audio quality/format key (validated in app.pipeline.AUDIO_FORMATS).
    default_audio_format: str = "mp3_320"
    destination_type: str = "browser"  # browser (ZIP download) | webdav (direct upload)
    language: str = "de"  # UI language code ("de" | "en"); see app.i18n.SUPPORTED_LANGUAGES

    # Which metadata fields to write (issue #7). All True = original behaviour;
    # see app.fix_music_tags.TagOptions / TAG_OPTION_FIELDS.
    tag_genre: bool = True
    tag_album_artist: bool = True
    tag_cover: bool = True
    tag_track_number: bool = True
    tag_feat_artist: bool = True
    tag_comments: bool = True

    webdav_url: str | None = None          # connection base URL
    webdav_folder: str | None = None       # chosen target sub-folder (relative to base)
    webdav_username: str | None = None
    webdav_password_enc: str | None = None  # Fernet-encrypted; never exposed in plaintext

    # Dedup (issue #31): skip tracks already in the user's library on a download, and
    # reference the existing copy in a playlist's .m3u8 instead of storing a duplicate.
    # WebDAV-only (a browser ZIP has no persistent library); default off (opt-in).
    dedup_skip_existing: bool = False

    # Synced lyrics (issue #43): fetch `.lrc` synced lyrics per track from LRCLIB and
    # write them as sidecar files (applies to ZIP and WebDAV); default off (opt-in).
    fetch_synced_lyrics: bool = False

    # Trash safety net (roadmap 01): when a library file is deleted via the ops layer
    # it's first moved into `.soundpull-trash/<date>/…` and hard-deleted only after this
    # many days. `0` = delete immediately (no trash). WebDAV-only.
    trash_retention_days: int = 30

    # Per-user YouTube cookie (issue #9), a Netscape cookies.txt fed to yt-dlp so
    # age-gated / bot-checked / throttled tracks download. Fernet-encrypted; never
    # exposed in plaintext to the client.
    youtube_cookies_enc: str | None = None

    # Notifications (issue #42): per-user push/webhook/e-mail alerts for background
    # events. All opt-in (default off), fanned out to whatever channels are configured.
    # Event toggles — chosen independently so the user controls exactly which events fire.
    notify_new_tracks: bool = False       # an interval-sync found new tracks
    notify_sync_error: bool = False       # a background interval-sync failed
    notify_download_error: bool = False   # a manual/artist download failed
    # ntfy push (simple HTTP POST to a topic URL; no SMTP needed).
    notify_ntfy_url: str | None = None            # e.g. https://ntfy.sh/my-topic
    notify_ntfy_token_enc: str | None = None      # optional Bearer token; Fernet-encrypted
    # Generic webhook (JSON POST) — keeps integrations flexible.
    notify_webhook_url: str | None = None
    # E-mail via SMTP (stdlib smtplib; no extra dependency).
    notify_email_to: str | None = None
    notify_smtp_host: str | None = None
    notify_smtp_port: int = 587
    notify_smtp_user: str | None = None
    notify_smtp_password_enc: str | None = None   # Fernet-encrypted
    notify_smtp_from: str | None = None
    notify_smtp_security: str = "starttls"        # "starttls" | "ssl" | "none"

    updated_at: datetime = Field(default_factory=_utcnow)

    user: User = Relationship(back_populates="settings")

    @property
    def has_webdav_password(self) -> bool:
        return bool(self.webdav_password_enc)

    @property
    def has_youtube_cookies(self) -> bool:
        return bool(self.youtube_cookies_enc)

    @property
    def has_ntfy_token(self) -> bool:
        return bool(self.notify_ntfy_token_enc)

    @property
    def has_smtp_password(self) -> bool:
        return bool(self.notify_smtp_password_enc)


class DownloadHistory(SQLModel, table=True):
    # id doubles as the in-process job / correlation id
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    url: str
    genre: str
    mode: str  # album | single | playlist | artist
    audio_format: str = "mp3_320"  # audio quality/format key (see app.pipeline)
    destination_type: str  # browser | webdav

    artist: str | None = None
    album: str | None = None
    phase: str = "queued"  # queued | metadata | download | tags | upload | done | error
    current_track: int = 0
    total_tracks: int = 0
    # Tracks that never completed after retries (throttle/403) + files the WebDAV server
    # rejected — the size of a silent partial delivery. 0 on a clean run; a non-zero value
    # pairs with the `jobs.partial_delivery` warning so the history shows "N von M" (#…).
    failed_tracks: int = 0
    error: str | None = None
    # Non-fatal note on a completed job (issue #38): e.g. the WebDAV upload succeeded but the
    # server-index update failed, so those tracks may be re-downloaded on the next sync. The
    # job still ends `done` (files are delivered); this just surfaces the risk in the history.
    warning: str | None = None
    # Human-readable event timeline of the job (issue #44): one line per phase/event
    # (queued → metadata → … → done/error), filled by the worker via `jobs._log_event`.
    # A technical trace like `error` — deliberately neutral/untranslated (the worker runs
    # off the request thread, so it has no session language). Shown in the detail dialog.
    log: str | None = None

    created_at: datetime = Field(default_factory=_utcnow, index=True)
    finished_at: datetime | None = None


class ServerTrack(SQLModel, table=True):
    """Per-user index of tracks known to be on the server (issue #21).

    The detector for playlist interval-sync: "does <artist> already have <title> on
    the server?" A row is the normalised (artist, title) of a track Soundpull has
    delivered to WebDAV (populated on every successful WebDAV upload; seedable via a
    directory scan). The keys are normalised by `app.library_index.track_key`, so a
    lookup matches regardless of feat-suffixes or the raw-vs-tagged artist form.

    `rel_path` (issue #31) is the delivered file's path RELATIVE TO the user's WebDAV
    base folder (`UserSettings.webdav_folder`), stored so a playlist can reference an
    already-present track by a cross-folder relative path in its .m3u8 instead of
    re-downloading a duplicate. It is nullable: rows seeded without a download
    (mark_existing) or from before this feature have no path (→ skip, but no reference).
    Caveat: the frame is `webdav_folder` at record time; if the user later changes that
    folder the stored path can be stale — the skip stays correct, only the reference may
    point wrong. Self-heals on the next full scan/sync.
    """
    __table_args__ = (
        UniqueConstraint("user_id", "artist_norm", "title_norm",
                         name="uq_servertrack_user_artist_title"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    artist_norm: str = Field(index=True)
    title_norm: str = Field(index=True)
    rel_path: str | None = None  # library-relative POSIX path; None if unknown (issue #31)
    created_at: datetime = Field(default_factory=_utcnow)


class PlaylistSubscription(SQLModel, table=True):
    """A playlist the user wants auto-synced on an interval (issue #21).

    The scheduler enqueues a sync every `interval_hours`; each sync downloads only
    tracks not already on the server (see `ServerTrack`) and uploads them via WebDAV
    (destination/credentials/tag-options are taken from the user's `UserSettings` at
    sync time — a subscription therefore requires WebDAV to be configured).
    """
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    url: str
    name: str = "Playlist"          # cached playlist title (for the UI)
    interval_hours: int = 24        # sync cadence
    enabled: bool = True
    genre: str = DEFAULT_GENRE
    audio_format: str = "mp3_320"
    # First run: "download_all" fetches everything now; "mark_existing" seeds the
    # index with the current playlist (no download) so only future additions arrive.
    initial_mode: str = "download_all"

    # Ordered manifest (JSON) of tracks this subscription placed into the playlist
    # folder, used to regenerate the complete `<name>.m3u8` on each incremental sync.
    playlist_files: str | None = None

    last_checked_at: datetime | None = None   # last scheduler evaluation / enqueue
    last_synced_at: datetime | None = None     # last successful sync
    last_status: str = "idle"                  # idle | ok | error
    last_error: str | None = None
    last_new_count: int = 0                    # tracks added by the last sync

    created_at: datetime = Field(default_factory=_utcnow, index=True)
