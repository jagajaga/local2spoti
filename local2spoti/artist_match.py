from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .matcher import Threshold, decide, score_candidate
from .models import LocalFile, MatchCandidate
from .spotify_client import SpotifyClient

ArtistDecision = Literal["auto", "review", "unmatched", "no_artist"]


@dataclass(slots=True)
class FileMatchResult:
    file: LocalFile
    decision: ArtistDecision
    top_candidate: MatchCandidate | None
    candidates: list[MatchCandidate]


def _track_to_candidate(
    track: dict,
    *,
    file: LocalFile,
    rank: int = 0,
) -> MatchCandidate | None:
    if not file.artist or not file.title:
        return None
    artists = track.get("artists", [{}])
    spotify_artist = artists[0].get("name", "") if artists else ""
    spotify_title = track.get("name", "")
    spotify_album = (track.get("album") or {}).get("name") if track.get("album") else None
    spotify_dur = track.get("duration_ms")
    s = score_candidate(
        local_artist=file.artist,
        local_title=file.title,
        local_album=file.album,
        local_duration_ms=file.duration_ms,
        spotify_artist=spotify_artist,
        spotify_title=spotify_title,
        spotify_album=spotify_album,
        spotify_duration_ms=spotify_dur,
    )
    return MatchCandidate(
        spotify_track_id=track["id"],
        spotify_artist=spotify_artist,
        spotify_title=spotify_title,
        spotify_album=spotify_album,
        spotify_duration_ms=spotify_dur,
        artist_similarity=s.artist_similarity,
        title_similarity=s.title_similarity,
        duration_delta_ms=s.duration_delta_ms,
        confidence=s.confidence,
        rank=rank,
        variant_mismatch=s.variant_mismatch,
    )


async def match_artist_group(
    *,
    client: SpotifyClient,
    artist: str,
    files: list[LocalFile],
    threshold: Threshold,
    conn=None,  # aiosqlite connection for the artist-catalog cache; optional
) -> list[FileMatchResult]:
    """Match every file in `files` against the Spotify catalog of `artist`.

    If `conn` is provided, hits the persistent artist_catalog cache first.
    On cache miss (or expiry) we fetch from Spotify and store the result;
    on cache hit we skip the search/albums/albums-batch call chain
    entirely and run the local rapidfuzz scoring against the cached
    track list.
    """
    catalog: list[dict] = []

    # Cache check — only when we have a DB handle (some test paths pass None)
    cached = None
    if conn is not None:
        from . import artist_cache

        cached = await artist_cache.get(conn, artist)

    if cached is not None:
        if not cached.is_positive:
            # Negative-result cache: Spotify had no match for this name
            # last time we asked, and the entry hasn't expired yet.
            return [FileMatchResult(f, "no_artist", None, []) for f in files]
        catalog = cached.tracks
    else:
        spotify_artist = await client.search_artist(artist)
        if spotify_artist is None:
            if conn is not None:
                from . import artist_cache

                await artist_cache.put(
                    conn,
                    artist,
                    spotify_artist_id=None,
                    spotify_artist_name=None,
                    tracks=[],
                )
            return [FileMatchResult(f, "no_artist", None, []) for f in files]

        albums = await client.artist_albums(spotify_artist["id"])
        album_ids = [a["id"] for a in albums]
        full_albums = await client.albums_batch(album_ids)

        seen_ids: set[str] = set()
        for alb in full_albums:
            for t in alb.get("tracks", {}).get("items", []):
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                t = {**t, "album": {"name": alb.get("name")}}
                catalog.append(t)

        if conn is not None:
            from . import artist_cache

            await artist_cache.put(
                conn,
                artist,
                spotify_artist_id=spotify_artist["id"],
                spotify_artist_name=spotify_artist.get("name"),
                tracks=catalog,
            )

    results: list[FileMatchResult] = []
    for f in files:
        scored = [c for c in (_track_to_candidate(t, file=f) for t in catalog) if c]
        scored.sort(key=lambda c: -c.confidence)
        top5 = scored[:5]
        for i, c in enumerate(top5, start=1):
            c.rank = i
        if not top5:
            results.append(FileMatchResult(f, "unmatched", None, []))
            continue
        top = top5[0]
        decision = decide(
            artist_sim=top.artist_similarity,
            title_sim=top.title_similarity,
            album_match=(top.spotify_album is not None and f.album is not None),
            duration_delta_ms=top.duration_delta_ms,
            threshold=threshold,
            variant_mismatch=top.variant_mismatch,
        )
        results.append(FileMatchResult(f, decision, top, top5))
    return results


async def match_per_track(
    *,
    client: SpotifyClient,
    files: list[LocalFile],
    threshold: Threshold,
) -> list[FileMatchResult]:
    out: list[FileMatchResult] = []
    for f in files:
        if not f.artist or not f.title:
            out.append(FileMatchResult(f, "unmatched", None, []))
            continue
        items = await client.search_tracks(f.artist, f.title, limit=5)
        scored = [c for c in (_track_to_candidate(t, file=f) for t in items) if c]
        scored.sort(key=lambda c: -c.confidence)
        top5 = scored[:5]
        for i, c in enumerate(top5, start=1):
            c.rank = i
        if not top5:
            out.append(FileMatchResult(f, "unmatched", None, []))
            continue
        top = top5[0]
        decision = decide(
            artist_sim=top.artist_similarity,
            title_sim=top.title_similarity,
            album_match=(top.spotify_album is not None and f.album is not None),
            duration_delta_ms=top.duration_delta_ms,
            threshold=threshold,
            variant_mismatch=top.variant_mismatch,
        )
        out.append(FileMatchResult(f, decision, top, top5))
    return out
