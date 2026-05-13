from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .normalize import similarity


class Threshold(StrEnum):
    STRICT = "strict"
    BALANCED = "balanced"
    LOOSE = "loose"


Decision = Literal["auto", "review", "unmatched"]

# Title markers that mean "this is a *variant* of the canonical recording".
# When a Spotify candidate has one of these and the local title does NOT,
# the candidate is almost certainly the wrong recording — e.g. local file
# is "Billy Idol — Mony Mony" but the candidate is "Mony Mony - Live at
# MSG" or "Mony Mony (2014 Remastered)". We apply a heavy penalty so the
# clean studio recording wins the ranking.
_VARIANT_MARKERS = re.compile(
    r"\b("
    r"live|"
    r"acoustic|"
    r"remix|"
    r"radio[- ]?edit|"
    r"single[- ]?edit|"
    r"extended[- ]?(?:mix|version|edit)?|"
    r"remaster(?:ed)?|"
    r"re[- ]?recorded|"
    r"karaoke|"
    r"instrumental|"
    r"a cappella|"
    r"demo|"
    r"mono(?: version)?|"
    r"unplugged|"
    r"deluxe(?: edition)?"
    r")\b",
    re.IGNORECASE,
)


def _has_variant_marker(s: str) -> bool:
    return bool(_VARIANT_MARKERS.search(s))


@dataclass(slots=True)
class Score:
    artist_similarity: float
    title_similarity: float
    album_match: bool
    duration_delta_ms: int | None
    confidence: float
    # True when the Spotify candidate's title contains a variant marker
    # (live/remix/remaster/...) that the local title doesn't. Used by
    # `decide` to refuse auto-matching variant versions even when the
    # string similarities clear the threshold — the local file is almost
    # always the studio recording, not the live cut, and a wrong
    # auto-match here would silently route a clean track to the wrong
    # version on Spotify.
    variant_mismatch: bool = False


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
    if local_album and spotify_album and similarity(local_album, spotify_album) >= 0.90:
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

    # Variant detection: Spotify's title has a live/remix/remaster
    # marker that the local file's title doesn't. The local file is
    # almost certainly the clean studio cut — penalize heavily so the
    # canonical recording outranks the variant in the candidates list.
    variant_mismatch = _has_variant_marker(spotify_title) and not _has_variant_marker(local_title)
    variant_penalty = 0.30 if variant_mismatch else 0.0

    confidence = 0.45 * a_sim + 0.45 * t_sim + album_bonus + dur_bonus - variant_penalty
    confidence = max(0.0, min(1.0, confidence))
    return Score(
        artist_similarity=a_sim,
        title_similarity=t_sim,
        album_match=album_match,
        duration_delta_ms=delta,
        confidence=confidence,
        variant_mismatch=variant_mismatch,
    )


def decide(
    *,
    artist_sim: float,
    title_sim: float,
    album_match: bool,
    duration_delta_ms: int | None,
    threshold: Threshold,
    variant_mismatch: bool = False,
) -> Decision:
    dur_within = duration_delta_ms is not None and duration_delta_ms <= 3000

    # Variant guard: never auto-match a live/remix/remaster candidate
    # when the local title doesn't say so. We'd rather route the file
    # to review and let the user pick than silently send a studio
    # listener to the live cut. score_candidate already penalizes
    # confidence by 0.30, but that alone isn't enough — a near-perfect
    # string-sim variant could still scrape past the auto threshold.
    if variant_mismatch:
        score = 0.45 * artist_sim + 0.45 * title_sim
        return "review" if score >= 0.50 else "unmatched"

    if threshold is Threshold.STRICT:
        if artist_sim >= 0.95 and title_sim >= 0.95 and dur_within:
            return "auto"
    elif threshold is Threshold.BALANCED:
        # Drop the album-match safety net (was: `album_match or
        # dur_within_5s`). In this user's library many files have no
        # album tag or it differs slightly from Spotify's reissue, so
        # the album path was rarely the decider anyway — duration is
        # the meaningful safety check. Tightened to within 3s (was
        # 5s) so remasters get caught and routed to review.
        if artist_sim >= 0.90 and title_sim >= 0.90 and dur_within:
            return "auto"
    else:  # LOOSE
        if artist_sim >= 0.80 and title_sim >= 0.80:
            return "auto"

    score = 0.45 * artist_sim + 0.45 * title_sim
    return "review" if score >= 0.50 else "unmatched"
