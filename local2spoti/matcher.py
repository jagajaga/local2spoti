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

# Title markers that mean "this is a *different recording* than the
# canonical studio cut" — different performance, different rendition.
# Triggers the variant_mismatch penalty: if the Spotify candidate has
# one of these and the local title doesn't, route to review so the
# user doesn't silently get the wrong recording.
_VARIANT_MARKERS = re.compile(
    r"\b("
    r"live|"
    r"acoustic|"
    r"remix|"
    r"radio[- ]?edit|"
    r"single[- ]?edit|"
    r"extended[- ]?(?:mix|version|edit)?|"
    r"re[- ]?recorded|"
    r"karaoke|"
    r"instrumental|"
    r"a cappella|"
    r"demo|"
    r"unplugged"
    r")\b",
    re.IGNORECASE,
)

# Markers that signal "same recording, different *release*". These
# should NOT trigger variant_mismatch — they're the same performance,
# just on a remastered/repackaged album. We do still strip them from
# the title for similarity computation so "Lazy Sunday" vs
# "Lazy Sunday (Mono Version) (2018 Remaster)" scores ~1.0 instead of
# being penalized for length divergence.
_REPACKAGE_MARKERS = re.compile(
    r"\b("
    r"remaster(?:ed)?|"
    r"mono(?: version)?|"
    r"stereo(?: version)?|"
    r"deluxe(?: edition)?|"
    r"anniversary(?: edition)?|"
    r"\d{4}\s*(?:remaster|version|edition)|"
    r"album version|"
    r"original (?:version|recording)"
    r")\b",
    re.IGNORECASE,
)

# Common decorative bracketed/parenthesized suffixes Spotify adds after
# the canonical title. We strip these before similarity compute so the
# local "Lazy Sunday" matches a Spotify "Lazy Sunday (2018 Remaster)".
_PAREN_SUFFIX = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_DASH_SUFFIX = re.compile(r"\s*-\s*[^-]+$")


def _has_variant_marker(s: str) -> bool:
    return bool(_VARIANT_MARKERS.search(s))


def _strip_repackage(s: str) -> str:
    """Remove repackage/remaster decorations from a track title so
    similarity scoring doesn't punish a same-recording remaster pair."""
    out = s
    # First strip trailing bracketed groups that match repackage markers.
    while True:
        m = _PAREN_SUFFIX.search(out)
        if not m:
            break
        inside = m.group(0)
        if _REPACKAGE_MARKERS.search(inside) or _VARIANT_MARKERS.search(inside):
            out = out[: m.start()].rstrip()
            continue
        break
    # Then strip a trailing " - X" if X is purely repackage markers.
    m = _DASH_SUFFIX.search(out)
    if m and _REPACKAGE_MARKERS.search(m.group(0)) and not _VARIANT_MARKERS.search(m.group(0)):
        out = out[: m.start()].rstrip()
    return out


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
    # Strip "(2018 Remaster)" / "(Mono Version)" / etc. from the
    # Spotify title before similarity so a same-recording remaster
    # doesn't get punished for length divergence. Variant markers
    # (live/remix/...) stay in — those mean a different recording.
    t_sim = similarity(local_title, _strip_repackage(spotify_title))

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
