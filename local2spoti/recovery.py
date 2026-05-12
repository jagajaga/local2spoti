"""Background-task bodies for the AcoustID and AI metadata-recovery passes.

Lives in its own module so the regular route handlers (`/api/deep_scan`,
`/api/ai_scan`) and the smart Start-scan flow (`/api/scan/start` running the
full pipeline) can reuse them without duplicating the loop bodies.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from . import repo
from .acoustid import AcoustidClient, AcoustidError, fingerprint
from .ai_match import AIClient
from .events import ProgressEvent
from .musicbrainz import MusicBrainzClient
from .songlink import SongLinkClient
from .spotify_client import SpotifyClient, SpotifyError

if TYPE_CHECKING:
    from .state import AppState


async def deep_scan_unmatched(
    state: AppState,
    *,
    limit: int = 100000,
    statuses: tuple[str, ...] = ("unmatched",),
) -> dict[str, int]:
    """Run AcoustID fingerprint + MusicBrainz lookup over every file whose
    status is in `statuses`.

    Default operates on 'unmatched' files (no Spotify match found). Pass
    ('review',) to re-fingerprint files in the review queue. Pass
    ('scanned', 'unmatched') to run the AcoustID + MB fast-path on
    everything that still needs matching — files that resolve via MB go
    straight to `status='matched'` with a Spotify track ID, no
    /v1/search call ever.

    Returns the per-outcome counts. Emits stage='deep_scan' progress events.
    Cancels cleanly on `state.cancel_event`. Aborts the whole run on the
    first AcoustID auth/quota error.
    """
    placeholders = ",".join("?" * len(statuses))
    cur = await state.db_conn.execute(
        f"SELECT id, path, duration_ms FROM local_file "
        f"WHERE status IN ({placeholders}) AND spotify_track_id IS NULL "
        f"LIMIT ?",
        (*statuses, limit),
    )
    rows = await cur.fetchall()
    outcomes = {
        "matched": 0,  # AcoustID identified artist/title (sent to Spotify search later)
        "mb_direct": 0,  # MusicBrainz had a Spotify URL → straight to status='matched'
        "odesli": 0,  # MB had a non-Spotify URL → Odesli resolved to Spotify
        "isrc": 0,  # MB had no useful URL but ISRC + Spotify ISRC index resolved it
        "no_match": 0,
        "fpcalc_failed": 0,
    }
    pool_label = "/".join(statuses)
    if not rows:
        await state.bus.publish(
            ProgressEvent(
                stage="deep_scan",
                processed=0,
                total=0,
                message=f"no {pool_label} files to deep-scan",
            )
        )
        return outcomes
    total = len(rows)
    processed = 0
    await state.bus.publish(
        ProgressEvent(
            stage="deep_scan",
            processed=0,
            total=total,
            message=f"fingerprinting {total} {pool_label} files",
        )
    )
    acoustid = AcoustidClient(api_key=state.settings.acoustid_api_key)
    # Resolves MBID → Spotify track ID via MusicBrainz URL relationships,
    # bypassing /v1/search entirely for tracks MB has registered. Free,
    # 1 req/sec rate-limited internally.
    musicbrainz = MusicBrainzClient()
    # Odesli/SongLink: when MB has an Apple/Deezer/Tidal/etc URL but no
    # Spotify URL, Odesli does the cross-platform translation. Free,
    # 10 rpm public limit honored internally.
    songlink = SongLinkClient()
    # Optional Spotify client — used for the inline ISRC fast-path: MB
    # often has the ISRC even when it lacks a Spotify URL relationship,
    # and Spotify's catalog index lets `q=isrc:XXX` resolve to the
    # exact track in one /search call. We only construct this if a
    # Spotify token is on file; otherwise the run gracefully falls back
    # to the URL-only paths (mb_direct + odesli). One /search per ISRC
    # is far less than the artist+albums chain we'd otherwise need.
    spotify: SpotifyClient | None = None
    cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    row = await cur.fetchone()
    if row:
        # Inline 401-aware token provider so a long fingerprint run that
        # crosses the 60-min token-lifetime boundary picks up the fresh
        # token from the DB (refresh_loop keeps it current). Same logic
        # routes/api.py uses; duplicated here only to avoid pulling
        # routes into the recovery import graph.
        from .token_refresh import refresh_if_expiring

        async def _provide() -> str:
            with contextlib.suppress(Exception):
                await refresh_if_expiring(
                    conn=state.db_conn,
                    client_id=state.settings.spotify_client_id,
                    threshold_seconds=86400,
                )
            cur2 = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
            r2 = await cur2.fetchone()
            return r2[0] if r2 else ""

        spotify = SpotifyClient(
            access_token=row[0],
            bucket=state.spotify_bucket,
            token_provider=_provide,
        )
    try:
        for fid, path_str, _dur_ms in rows:
            if state.cancel_event.is_set():
                await state.bus.publish(
                    ProgressEvent(
                        stage="deep_scan",
                        processed=processed,
                        total=total,
                        message="cancelled",
                    )
                )
                return outcomes
            try:
                await _process_one_file(
                    state,
                    fid,
                    path_str,
                    statuses,
                    processed,
                    total,
                    outcomes=outcomes,
                    acoustid=acoustid,
                    musicbrainz=musicbrainz,
                    songlink=songlink,
                    spotify=spotify,
                )
            except AcoustidError as err:
                # Auth/quota errors aren't recoverable per-file — abort
                # the whole run so the user gets the message.
                await state.bus.publish(
                    ProgressEvent(
                        stage="deep_scan",
                        processed=processed,
                        total=total,
                        message=f"AcoustID error {err.code}: {err.message} — aborting",
                    )
                )
                return outcomes
            except Exception as exc:
                # Any other per-file failure (network blip the underlying
                # client somehow let through, malformed response, etc.)
                # gets logged into outcomes['no_match'] and the loop
                # continues. Without this, one rogue exception used to
                # kill the entire 11K-file run silently.
                outcomes["no_match"] += 1
                short = str(exc)[:120]
                await state.bus.publish(
                    ProgressEvent(
                        stage="deep_scan",
                        processed=processed,
                        total=total,
                        message=f"{path_str.rsplit('/', 1)[-1][:50]}: {type(exc).__name__} — skipped ({short})",
                    )
                )
            processed += 1
            await state.bus.publish(
                ProgressEvent(
                    stage="deep_scan",
                    processed=processed,
                    total=total,
                    message=(
                        f"mb_direct {outcomes['mb_direct']} / "
                        f"odesli {outcomes['odesli']} / "
                        f"isrc {outcomes['isrc']} / "
                        f"acoustid {outcomes['matched']} / "
                        f"no_match {outcomes['no_match']} / "
                        f"fpcalc_failed {outcomes['fpcalc_failed']}"
                    ),
                )
            )
        # The original tail of the loop did this final summary; we
        # moved it out of the per-file try/except so an exception
        # mid-batch couldn't skip the publish.
        await state.bus.publish(
            ProgressEvent(
                stage="deep_scan",
                processed=total,
                total=total,
                message=(
                    f"done — {outcomes['mb_direct']} matched via MusicBrainz, "
                    f"{outcomes['odesli']} via Odesli, "
                    f"{outcomes['isrc']} via Spotify ISRC index, "
                    f"{outcomes['matched']} acoustid-tagged awaiting match, "
                    f"{outcomes['no_match']} no_match, "
                    f"{outcomes['fpcalc_failed']} fpcalc_failed"
                ),
            )
        )
    finally:
        await acoustid.aclose()
        await musicbrainz.aclose()
        await songlink.aclose()
        if spotify is not None:
            await spotify.aclose()
        await state.bus.flush()
    return outcomes


async def _process_one_file(
    state: AppState,
    fid: int,
    path_str: str,
    statuses: tuple[str, ...],
    processed: int,
    total: int,
    *,
    outcomes: dict[str, int],
    acoustid: AcoustidClient,
    musicbrainz: MusicBrainzClient,
    songlink: SongLinkClient,
    spotify: SpotifyClient | None = None,
) -> None:
    """Process a single file: fpcalc → AcoustID → MB → Odesli → DB write.

    Pulled out of the main loop so per-file try/except can wrap it
    cleanly. Modifies `outcomes` in place. Re-raises AcoustidError
    (caller aborts the whole run on auth/quota errors).
    """
    cur2 = await state.db_conn.execute(
        "SELECT status FROM local_file WHERE id=?",
        (fid,),
    )
    row2 = await cur2.fetchone()
    # Race guard: skip if a parallel job already moved this file
    # out of the pool we're sweeping.
    if row2 is None or row2[0] not in statuses:
        return
    # Emit "fingerprinting <basename>" before the (potentially slow)
    # fpcalc + AcoustID + MB chain so the bar shows what we're
    # working on right now.
    current_name = path_str.rsplit("/", 1)[-1][:60]
    await state.bus.publish(
        ProgressEvent(
            stage="deep_scan",
            processed=processed,
            total=total,
            message=f"fingerprinting: {current_name}",
        )
    )
    fp = await fingerprint(Path(path_str))
    if fp is None:
        outcomes["fpcalc_failed"] += 1
        return
    dur, fp_str = fp
    md = await acoustid.lookup(fingerprint=fp_str, duration=dur)
    if md is None:
        outcomes["no_match"] += 1
        return
    # Try the MB → Spotify URL shortcut first. If MB has no Spotify URL
    # but does have an Apple/Deezer/Tidal URL, ask Odesli to convert it.
    # Either path lands directly on status='matched' with a Spotify
    # track ID, no /v1/search call ever.
    spotify_track_id: str | None = None
    match_method = "musicbrainz"
    metadata_source = "musicbrainz"
    isrc: str | None = None
    if md.recording_id:
        mb_res = await musicbrainz.resolve_mbid(md.recording_id)
        isrc = mb_res.isrc
        if mb_res.spotify_track_id:
            spotify_track_id = mb_res.spotify_track_id
        elif mb_res.odesli_url:
            spotify_track_id = await songlink.spotify_track_id_from_url(
                mb_res.odesli_url,
            )
            if spotify_track_id:
                match_method = "odesli"
                metadata_source = "odesli"
        # MB's url-rels coverage is partial — most recordings without a
        # Spotify URL still have an ISRC. When that's our last lever and
        # a Spotify client is available, ask Spotify's ISRC index
        # directly. Costs one /search per file, but the response is
        # deterministic (single track) and avoids the otherwise-required
        # full artist+albums chain.
        if not spotify_track_id and isrc and spotify is not None:
            try:
                track = await spotify.search_track_by_isrc(isrc)
            except SpotifyError:
                track = None
            if track:
                spotify_track_id = track["id"]
                match_method = "isrc"
                metadata_source = "isrc"
    await repo.clear_candidates(state.db_conn, fid)
    if spotify_track_id:
        await state.db_conn.execute(
            """UPDATE local_file SET
                artist=?, title=?, isrc=COALESCE(?, isrc),
                spotify_track_id=?, match_confidence=1.0,
                match_method=?,
                status='matched',
                metadata_source=?
               WHERE id=?""",
            (md.artist, md.title, isrc, spotify_track_id, match_method, metadata_source, fid),
        )
        if match_method == "odesli":
            outcomes["odesli"] += 1
        elif match_method == "isrc":
            outcomes["isrc"] += 1
        else:
            outcomes["mb_direct"] += 1
    else:
        # Persist the ISRC even when we couldn't get a Spotify URL —
        # the next /api/match run's ISRC pre-pass will resolve it via
        # Spotify's catalog index in one deterministic /search call.
        # COALESCE keeps any existing ISRC if MB didn't return one.
        await state.db_conn.execute(
            """UPDATE local_file SET artist=?, title=?,
                isrc=COALESCE(?, isrc),
                status='scanned', metadata_source='acoustid'
               WHERE id=?""",
            (md.artist, md.title, isrc, fid),
        )
        outcomes["matched"] += 1
    await state.db_conn.commit()


async def match_via_mb_text(
    state: AppState,
    *,
    limit: int = 100000,
    min_score: int = 80,
) -> dict[str, int]:
    """Resolve files to Spotify URLs via MusicBrainz text search.

    For each `status='scanned'` file with artist + title set (no
    Spotify match yet), do a MB recording text search. If the top
    result clears `min_score`, run the existing MBID-resolution chain
    (Spotify URL → Odesli → Spotify ISRC) to land directly on
    `status='matched'`.

    Use case: Spotify /search is rate-limited / 403'd and we want to
    keep matching. MB's rate limit is gentler (1 req/sec) and entirely
    independent of Spotify's, so this stage keeps moving even when the
    Spotify bucket is paused.

    Coverage is lower than Spotify /search (MB text search misses on
    typo'd / decorated titles), so this is additive — files MB can't
    find stay in `scanned` for the next /api/match run.

    Returns per-outcome counts. Emits stage='mb_text' progress events.
    """
    cur = await state.db_conn.execute(
        "SELECT id, path, artist, title, album, isrc FROM local_file "
        "WHERE status='scanned' AND spotify_track_id IS NULL "
        "AND artist IS NOT NULL AND title IS NOT NULL "
        "LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    outcomes = {
        "mb_direct": 0,  # MB had a Spotify URL
        "odesli": 0,  # MB → Apple/Deezer/Tidal → Odesli → Spotify
        "isrc": 0,  # MB had ISRC + Spotify ISRC index resolved it
        "isrc_tagged": 0,  # MB only gave us an ISRC; persisted, awaits next match
        "no_match": 0,  # MB text search found nothing scoring high enough
    }
    if not rows:
        await state.bus.publish(
            ProgressEvent(
                stage="mb_text",
                processed=0,
                total=0,
                message="no scanned files with artist+title to MB-text-search",
            )
        )
        return outcomes
    total = len(rows)
    processed = 0
    await state.bus.publish(
        ProgressEvent(
            stage="mb_text",
            processed=0,
            total=total,
            message=f"MB-text-searching {total} files",
        )
    )
    musicbrainz = MusicBrainzClient()
    songlink = SongLinkClient()
    spotify: SpotifyClient | None = None
    cur2 = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    row = await cur2.fetchone()
    if row:
        from .token_refresh import refresh_if_expiring

        async def _provide() -> str:
            with contextlib.suppress(Exception):
                await refresh_if_expiring(
                    conn=state.db_conn,
                    client_id=state.settings.spotify_client_id,
                    threshold_seconds=86400,
                )
            cur3 = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
            r3 = await cur3.fetchone()
            return r3[0] if r3 else ""

        spotify = SpotifyClient(
            access_token=row[0],
            bucket=state.spotify_bucket,
            token_provider=_provide,
        )
    try:
        for fid, path_str, artist, title, album, _existing_isrc in rows:
            if state.cancel_event.is_set():
                await state.bus.publish(
                    ProgressEvent(
                        stage="mb_text",
                        processed=processed,
                        total=total,
                        message="cancelled",
                    )
                )
                return outcomes
            current_name = path_str.rsplit("/", 1)[-1][:60]
            await state.bus.publish(
                ProgressEvent(
                    stage="mb_text",
                    processed=processed,
                    total=total,
                    message=f"searching: {current_name}",
                )
            )
            try:
                mbid = await musicbrainz.search_recording(
                    artist=artist,
                    title=title,
                    album=album,
                    min_score=min_score,
                )
            except Exception:
                mbid = None
            if mbid is None:
                outcomes["no_match"] += 1
                processed += 1
                continue
            try:
                mb_res = await musicbrainz.resolve_mbid(mbid)
            except Exception:
                outcomes["no_match"] += 1
                processed += 1
                continue
            spotify_track_id: str | None = mb_res.spotify_track_id
            match_method = "musicbrainz"
            metadata_source = "musicbrainz"
            if not spotify_track_id and mb_res.odesli_url:
                try:
                    spotify_track_id = await songlink.spotify_track_id_from_url(
                        mb_res.odesli_url,
                    )
                except Exception:
                    spotify_track_id = None
                if spotify_track_id:
                    match_method = "odesli"
                    metadata_source = "odesli"
            if not spotify_track_id and mb_res.isrc and spotify is not None:
                try:
                    track = await spotify.search_track_by_isrc(mb_res.isrc)
                except SpotifyError:
                    track = None
                if track:
                    spotify_track_id = track["id"]
                    match_method = "isrc"
                    metadata_source = "isrc"
            await repo.clear_candidates(state.db_conn, fid)
            if spotify_track_id:
                await state.db_conn.execute(
                    """UPDATE local_file SET
                        isrc=COALESCE(?, isrc),
                        spotify_track_id=?, match_confidence=1.0,
                        match_method=?, status='matched',
                        metadata_source=?
                       WHERE id=?""",
                    (mb_res.isrc, spotify_track_id, match_method, metadata_source, fid),
                )
                if match_method == "odesli":
                    outcomes["odesli"] += 1
                elif match_method == "isrc":
                    outcomes["isrc"] += 1
                else:
                    outcomes["mb_direct"] += 1
            else:
                # MB found the recording but no Spotify path resolved.
                # Persist the ISRC for the next /api/match ISRC pre-pass.
                if mb_res.isrc:
                    await state.db_conn.execute(
                        """UPDATE local_file SET isrc=COALESCE(?, isrc)
                           WHERE id=?""",
                        (mb_res.isrc, fid),
                    )
                    outcomes["isrc_tagged"] += 1
                else:
                    outcomes["no_match"] += 1
            await state.db_conn.commit()
            processed += 1
            await state.bus.publish(
                ProgressEvent(
                    stage="mb_text",
                    processed=processed,
                    total=total,
                    message=(
                        f"mb_direct {outcomes['mb_direct']} / "
                        f"odesli {outcomes['odesli']} / "
                        f"isrc {outcomes['isrc']} / "
                        f"isrc_tagged {outcomes['isrc_tagged']} / "
                        f"no_match {outcomes['no_match']}"
                    ),
                )
            )
        await state.bus.publish(
            ProgressEvent(
                stage="mb_text",
                processed=total,
                total=total,
                message=(
                    f"done — {outcomes['mb_direct']} mb_direct, "
                    f"{outcomes['odesli']} odesli, "
                    f"{outcomes['isrc']} isrc-resolved, "
                    f"{outcomes['isrc_tagged']} isrc-tagged-only, "
                    f"{outcomes['no_match']} no_match"
                ),
            )
        )
    finally:
        await musicbrainz.aclose()
        await songlink.aclose()
        if spotify is not None:
            await spotify.aclose()
        await state.bus.flush()
    return outcomes


async def ai_scan_unmatched(
    state: AppState,
    *,
    batch_size: int = 20,
    limit: int = 100000,
    status: str = "unmatched",
) -> dict[str, int]:
    """Run Claude metadata identification over every file with `status=<status>`.

    Default operates on 'unmatched'. Pass status='review' to ask Claude to
    re-identify files in the review queue (replaces existing artist/title +
    drops their stale candidates so the next match generates fresh ones).

    Returns by-confidence + updated counts. Emits stage='ai_scan' progress.
    Aborts the whole run on the first SDK exception (likely auth/quota).
    """
    cur = await state.db_conn.execute(
        """SELECT id, path, artist, title, album FROM local_file
           WHERE status=? LIMIT ?""",
        (status, limit),
    )
    rows = await cur.fetchall()
    by_confidence: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    if not rows:
        await state.bus.publish(
            ProgressEvent(
                stage="ai_scan",
                processed=0,
                total=0,
                message=f"no {status} files for AI scan",
            )
        )
        return {"updated": 0, **by_confidence}
    files = [{"id": r[0], "path": r[1], "artist": r[2], "title": r[3], "album": r[4]} for r in rows]
    total = len(files)
    updated = 0
    processed = 0
    await state.bus.publish(
        ProgressEvent(
            stage="ai_scan",
            processed=0,
            total=total,
            message=f"sending {total} {status} files to Claude in batches of {batch_size}",
        )
    )
    ai = AIClient()
    try:
        for i in range(0, total, batch_size):
            if state.cancel_event.is_set():
                await state.bus.publish(
                    ProgressEvent(
                        stage="ai_scan",
                        processed=processed,
                        total=total,
                        message="cancelled",
                    )
                )
                return {"updated": updated, **by_confidence}
            batch = files[i : i + batch_size]
            ids = [f["id"] for f in batch]
            placeholders = ",".join("?" * len(ids))
            cur2 = await state.db_conn.execute(
                f"SELECT id FROM local_file WHERE id IN ({placeholders}) AND status=?",
                (*ids, status),
            )
            still_unmatched = {r[0] for r in await cur2.fetchall()}
            skipped = len(batch) - len(still_unmatched)
            batch = [f for f in batch if f["id"] in still_unmatched]
            if not batch:
                processed += skipped
                continue
            try:
                suggestions = await ai.suggest_metadata(batch)
            except Exception as e:
                await state.bus.publish(
                    ProgressEvent(
                        stage="ai_scan",
                        processed=processed,
                        total=total,
                        message=f"failed: {e}",
                    )
                )
                return {"updated": updated, **by_confidence}
            for s in suggestions:
                by_confidence[s.confidence] = by_confidence.get(s.confidence, 0) + 1
                if s.usable:
                    await repo.clear_candidates(state.db_conn, s.file_id)
                    await state.db_conn.execute(
                        """UPDATE local_file SET artist=?, title=?, album=?,
                           status='scanned', metadata_source='ai' WHERE id=?""",
                        (s.artist, s.title, s.album, s.file_id),
                    )
                    updated += 1
            await state.db_conn.commit()
            processed += len(batch) + skipped
            await state.bus.publish(
                ProgressEvent(
                    stage="ai_scan",
                    processed=processed,
                    total=total,
                    message=(
                        f"updated {updated}, "
                        f"high {by_confidence['high']} / "
                        f"medium {by_confidence['medium']} / "
                        f"low {by_confidence['low']} / "
                        f"none {by_confidence['none']}"
                    ),
                )
            )
        await state.bus.publish(
            ProgressEvent(
                stage="ai_scan",
                processed=total,
                total=total,
                message=f"done — {updated} files have AI metadata",
            )
        )
    finally:
        await ai.aclose()
        await state.bus.flush()
    return {"updated": updated, **by_confidence}


async def count_unmatched(conn) -> int:
    cur = await conn.execute("SELECT COUNT(*) FROM local_file WHERE status='unmatched'")
    (n,) = await cur.fetchone()
    return n


async def auto_cycle(
    state: AppState,
    *,
    max_iterations: int = 10,
    ai_batch_size: int = 20,
) -> dict[str, int]:
    """Drive match → AI(review) → AI(unmatched) → match … in a loop.

    Stops when an iteration moves zero files (everything that could be
    rescued has been) or after `max_iterations` (safety cap so a bug or
    AI quota-out can't loop forever). Each iteration emits a stage=
    'auto_cycle' progress event so the dashboard can show the loop.

    Designed for the common power-user flow: after fingerprint + MB-text
    + manual fixes, press one button to chew through the long tail of
    review/unmatched files, with Claude re-identifying what Spotify
    couldn't.
    """
    from datetime import UTC, datetime

    from .matcher import Threshold
    from .pipeline import _stage_match
    from .spotify_client import SpotifyClient
    from .token_refresh import refresh_if_expiring

    # Build an authenticated Spotify client up front. We pass it into
    # _stage_match each iteration; if the token's missing we abort
    # early rather than burn through AI scans the user can't push.
    cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    row = await cur.fetchone()
    if not row:
        await state.bus.publish(
            ProgressEvent(
                stage="auto_cycle",
                processed=0,
                total=0,
                message="Spotify not connected — connect first, then retry auto-cycle",
            )
        )
        return {"iterations": 0, "matched_delta": 0}

    async def _provide() -> str:
        try:
            await refresh_if_expiring(
                conn=state.db_conn,
                client_id=state.settings.spotify_client_id,
                threshold_seconds=86400,
            )
        except Exception:
            pass
        cur2 = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
        r2 = await cur2.fetchone()
        return r2[0] if r2 else ""

    client = SpotifyClient(
        access_token=row[0],
        bucket=state.spotify_bucket,
        token_provider=_provide,
    )

    async def _matched_count() -> int:
        c = await state.db_conn.execute("SELECT COUNT(*) FROM local_file WHERE status='matched'")
        (n,) = await c.fetchone()
        return n

    threshold = Threshold(state.settings.threshold)
    start_matched = await _matched_count()
    total_iters = 0
    try:
        for i in range(1, max_iterations + 1):
            if state.cancel_event.is_set():
                await state.bus.publish(
                    ProgressEvent(
                        stage="auto_cycle",
                        processed=total_iters,
                        total=max_iterations,
                        message=f"cancelled after iteration {total_iters}",
                    )
                )
                break
            before = await _matched_count()
            await state.bus.publish(
                ProgressEvent(
                    stage="auto_cycle",
                    processed=i - 1,
                    total=max_iterations,
                    message=f"iter {i}/{max_iterations}: running Spotify match",
                )
            )
            await _stage_match(
                state.db_conn,
                client,
                threshold,
                bus=state.bus,
                now=datetime.now(UTC),
            )
            if state.cancel_event.is_set():
                break
            await state.bus.publish(
                ProgressEvent(
                    stage="auto_cycle",
                    processed=i - 1,
                    total=max_iterations,
                    message=f"iter {i}/{max_iterations}: Claude AI on review queue",
                )
            )
            await ai_scan_unmatched(state, batch_size=ai_batch_size, status="review")
            if state.cancel_event.is_set():
                break
            await state.bus.publish(
                ProgressEvent(
                    stage="auto_cycle",
                    processed=i - 1,
                    total=max_iterations,
                    message=f"iter {i}/{max_iterations}: Claude AI on unmatched",
                )
            )
            await ai_scan_unmatched(state, batch_size=ai_batch_size, status="unmatched")
            after = await _matched_count()
            delta = after - before
            total_iters = i
            await state.bus.publish(
                ProgressEvent(
                    stage="auto_cycle",
                    processed=i,
                    total=max_iterations,
                    message=f"iter {i}/{max_iterations} done — +{delta} matched this cycle",
                )
            )
            if delta == 0:
                # Stable: nothing the loop can rescue still moves. Done.
                await state.bus.publish(
                    ProgressEvent(
                        stage="auto_cycle",
                        processed=i,
                        total=max_iterations,
                        message=f"converged — no new matches in iter {i}, stopping",
                    )
                )
                break
    finally:
        await client.aclose()
        await state.bus.flush()

    end_matched = await _matched_count()
    return {"iterations": total_iters, "matched_delta": end_matched - start_matched}
