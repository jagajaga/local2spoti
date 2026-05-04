from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .normalize import similarity


class Threshold(str, Enum):
    STRICT = "strict"
    BALANCED = "balanced"
    LOOSE = "loose"


Decision = Literal["auto", "review", "unmatched"]


@dataclass(slots=True)
class Score:
    artist_similarity: float
    title_similarity: float
    album_match: bool
    duration_delta_ms: int | None
    confidence: float


def score_candidate(
    *,
    local_artist: str,
    local_title: str,
    local_album: str | None,
    local_duration_ms: int | None,
    spotify_artist: str,
    spotify_title: str,
    spotify_album: str | None,
    spotify_duration_ms: int | None,
) -> Score:
    a_sim = similarity(local_artist, spotify_artist)
    t_sim = similarity(local_title, spotify_title)

    album_match = False
    album_bonus = 0.0
    if local_album and spotify_album:
        if similarity(local_album, spotify_album) >= 0.90:
            album_match = True
            album_bonus = 0.05

    delta: int | None = None
    dur_bonus = 0.0
    if local_duration_ms and spotify_duration_ms:
        delta = abs(local_duration_ms - spotify_duration_ms)
        if delta <= 3000:
            dur_bonus = 0.10
        elif delta <= 7000:
            dur_bonus = 0.05

    confidence = 0.45 * a_sim + 0.45 * t_sim + album_bonus + dur_bonus
    confidence = min(1.0, confidence)
    return Score(
        artist_similarity=a_sim,
        title_similarity=t_sim,
        album_match=album_match,
        duration_delta_ms=delta,
        confidence=confidence,
    )


def decide(
    *,
    artist_sim: float,
    title_sim: float,
    album_match: bool,
    duration_delta_ms: int | None,
    threshold: Threshold,
) -> Decision:
    dur_within = duration_delta_ms is not None and duration_delta_ms <= 3000
    dur_within_5s = duration_delta_ms is not None and duration_delta_ms <= 5000

    if threshold is Threshold.STRICT:
        if artist_sim >= 0.95 and title_sim >= 0.95 and dur_within:
            return "auto"
    elif threshold is Threshold.BALANCED:
        if artist_sim >= 0.90 and title_sim >= 0.90 and (album_match or dur_within_5s):
            return "auto"
    else:  # LOOSE
        if artist_sim >= 0.80 and title_sim >= 0.80:
            return "auto"

    score = 0.45 * artist_sim + 0.45 * title_sim
    return "review" if score >= 0.50 else "unmatched"
