from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FileStatus(StrEnum):
    NEW = "new"
    SCANNED = "scanned"
    MATCHED = "matched"
    REVIEW = "review"
    UNMATCHED = "unmatched"
    ERROR = "error"
    MISSING = "missing"


class MetadataSource(StrEnum):
    TAGS = "tags"
    FILENAME = "filename"
    ACOUSTID = "acoustid"
    AI = "ai"
    MANUAL = "manual"
    MUSICBRAINZ = "musicbrainz"  # Spotify track ID resolved via MB URL relationships
    NONE = "none"


@dataclass(slots=True)
class LocalFile:
    path: str
    mtime: int
    size: int
    format: str
    id: int | None = None
    duration_ms: int | None = None
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    track_number: int | None = None
    isrc: str | None = None
    metadata_source: str | None = None
    status: FileStatus = FileStatus.NEW
    spotify_track_id: str | None = None
    match_confidence: float | None = None
    match_method: str | None = None
    first_seen_at: str | None = None
    last_scanned_at: str | None = None
    last_error: str | None = None
    last_run_id: int | None = None


@dataclass(order=True)
class MatchCandidate:
    sort_index: float = field(init=False, repr=False)
    spotify_track_id: str = ""
    spotify_artist: str = ""
    spotify_title: str = ""
    artist_similarity: float = 0.0
    title_similarity: float = 0.0
    confidence: float = 0.0
    rank: int = 0
    spotify_album: str | None = None
    spotify_duration_ms: int | None = None
    duration_delta_ms: int | None = None

    def __post_init__(self) -> None:
        self.sort_index = -self.confidence


@dataclass(slots=True)
class PlaylistChunk:
    id: int | None
    spotify_playlist_id: str | None
    name: str
    chunk_index: int
    alpha_range: str
    track_count: int = 0


@dataclass(slots=True)
class ScanProgress:
    stage: str
    processed: int
    total: int
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    errors: int = 0
