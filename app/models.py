"""SQLModel tables: User, UserSettings, DownloadHistory.

Note: deliberately NO `from __future__ import annotations` here — SQLModel
resolves quoted relationship forward refs (e.g. "UserSettings") via its own
registry, and the future import would double-stringify them and break mapping.
"""
from datetime import datetime, timezone
from typing import Optional

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
    default_mode: str = "album"  # album | single | playlist
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

    # Per-user YouTube cookie (issue #9), a Netscape cookies.txt fed to yt-dlp so
    # age-gated / bot-checked / throttled tracks download. Fernet-encrypted; never
    # exposed in plaintext to the client.
    youtube_cookies_enc: str | None = None

    updated_at: datetime = Field(default_factory=_utcnow)

    user: User = Relationship(back_populates="settings")

    @property
    def has_webdav_password(self) -> bool:
        return bool(self.webdav_password_enc)

    @property
    def has_youtube_cookies(self) -> bool:
        return bool(self.youtube_cookies_enc)


class DownloadHistory(SQLModel, table=True):
    # id doubles as the in-process job / correlation id
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    url: str
    genre: str
    mode: str  # album | single | playlist
    audio_format: str = "mp3_320"  # audio quality/format key (see app.pipeline)
    destination_type: str  # browser | webdav

    artist: str | None = None
    album: str | None = None
    phase: str = "queued"  # queued | metadata | download | tags | upload | done | error
    current_track: int = 0
    total_tracks: int = 0
    error: str | None = None

    created_at: datetime = Field(default_factory=_utcnow, index=True)
    finished_at: datetime | None = None
