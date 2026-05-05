"""Background-task bodies for the AcoustID and AI metadata-recovery passes.

Lives in its own module so the regular route handlers (`/api/deep_scan`,
`/api/ai_scan`) and the smart Start-scan flow (`/api/scan/start` running the
full pipeline) can reuse them without duplicating the loop bodies.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from . import repo
from .acoustid import AcoustidClient, AcoustidError, fingerprint
from .ai_match import AIClient
from .events import ProgressEvent
from .musicbrainz import MusicBrainzClient
from .songlink import SongLinkClient

if TYPE_CHECKING:
    from .state import AppState


async def deep_scan_unmatched(
    state: "AppState", *, limit: int = 100000,
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
        "matched": 0,        # AcoustID identified artist/title (sent to Spotify search later)
        "mb_direct": 0,      # MusicBrainz had a Spotify URL → straight to status='matched'
        "odesli": 0,         # MB had a non-Spotify URL → Odesli resolved to Spotify
        "no_match": 0,
        "fpcalc_failed": 0,
    }
    pool_label = "/".join(statuses)
    if not rows:
        await state.bus.publish(ProgressEvent(
            stage="deep_scan", processed=0, total=0,
            message=f"no {pool_label} files to deep-scan",
        ))
        return outcomes
    total = len(rows)
    processed = 0
    await state.bus.publish(ProgressEvent(
        stage="deep_scan", processed=0, total=total,
        message=f"fingerprinting {total} {pool_label} files",
    ))
    acoustid = AcoustidClient(api_key=state.settings.acoustid_api_key)
    # Resolves MBID → Spotify track ID via MusicBrainz URL relationships,
    # bypassing /v1/search entirely for tracks MB has registered. Free,
    # 1 req/sec rate-limited internally.
    musicbrainz = MusicBrainzClient()
    # Odesli/SongLink: when MB has an Apple/Deezer/Tidal/etc URL but no
    # Spotify URL, Odesli does the cross-platform translation. Free,
    # 10 rpm public limit honored internally.
    songlink = SongLinkClient()
    try:
        for fid, path_str, _dur_ms in rows:
            if state.cancel_event.is_set():
                await state.bus.publish(ProgressEvent(
                    stage="deep_scan", processed=processed, total=total,
                    message="cancelled",
                ))
                return outcomes
            cur2 = await state.db_conn.execute(
                "SELECT status FROM local_file WHERE id=?", (fid,),
            )
            row2 = await cur2.fetchone()
            # Race guard: skip if a parallel job already moved this file
            # out of the pool we're sweeping.
            if row2 is None or row2[0] not in statuses:
                processed += 1
                continue
            # Emit "fingerprinting <basename>" before the (potentially
            # slow) fpcalc + AcoustID + MB chain so the bar shows what
            # we're working on right now, not just totals from the last
            # completed file. Truncated to the basename to keep messages
            # short on the dashboard.
            current_name = path_str.rsplit("/", 1)[-1][:60]
            await state.bus.publish(ProgressEvent(
                stage="deep_scan", processed=processed, total=total,
                message=f"fingerprinting: {current_name}",
            ))
            fp = await fingerprint(Path(path_str))
            if fp is None:
                outcomes["fpcalc_failed"] += 1
            else:
                dur, fp_str = fp
                try:
                    md = await acoustid.lookup(fingerprint=fp_str, duration=dur)
                except AcoustidError as err:
                    await state.bus.publish(ProgressEvent(
                        stage="deep_scan", processed=processed, total=total,
                        message=f"AcoustID error {err.code}: {err.message} — aborting",
                    ))
                    return outcomes
                if md is None:
                    outcomes["no_match"] += 1
                else:
                    # Try the MB → Spotify URL shortcut first. If MB has
                    # no Spotify URL but does have an Apple/Deezer/Tidal
                    # URL, ask Odesli to convert it. Either path lands
                    # directly on status='matched' with a Spotify track
                    # ID, no /v1/search call ever.
                    spotify_track_id: str | None = None
                    match_method = "musicbrainz"
                    metadata_source = "musicbrainz"
                    if md.recording_id:
                        mb_res = await musicbrainz.resolve_mbid(md.recording_id)
                        if mb_res.spotify_track_id:
                            spotify_track_id = mb_res.spotify_track_id
                        elif mb_res.odesli_url:
                            spotify_track_id = await songlink.spotify_track_id_from_url(
                                mb_res.odesli_url,
                            )
                            if spotify_track_id:
                                match_method = "odesli"
                                metadata_source = "odesli"
                    await repo.clear_candidates(state.db_conn, fid)
                    if spotify_track_id:
                        await state.db_conn.execute(
                            """UPDATE local_file SET
                                artist=?, title=?,
                                spotify_track_id=?, match_confidence=1.0,
                                match_method=?,
                                status='matched',
                                metadata_source=?
                               WHERE id=?""",
                            (md.artist, md.title, spotify_track_id,
                             match_method, metadata_source, fid),
                        )
                        if match_method == "odesli":
                            outcomes["odesli"] += 1
                        else:
                            outcomes["mb_direct"] += 1
                    else:
                        await state.db_conn.execute(
                            """UPDATE local_file SET artist=?, title=?,
                                status='scanned', metadata_source='acoustid'
                               WHERE id=?""",
                            (md.artist, md.title, fid),
                        )
                        outcomes["matched"] += 1
                    await state.db_conn.commit()
            processed += 1
            await state.bus.publish(ProgressEvent(
                stage="deep_scan", processed=processed, total=total,
                message=(
                    f"mb_direct {outcomes['mb_direct']} / "
                    f"odesli {outcomes['odesli']} / "
                    f"acoustid {outcomes['matched']} / "
                    f"no_match {outcomes['no_match']} / "
                    f"fpcalc_failed {outcomes['fpcalc_failed']}"
                ),
            ))
        await state.bus.publish(ProgressEvent(
            stage="deep_scan", processed=total, total=total,
            message=(
                f"done — {outcomes['mb_direct']} matched via MusicBrainz, "
                f"{outcomes['odesli']} via Odesli (no Spotify search), "
                f"{outcomes['matched']} acoustid-tagged awaiting match, "
                f"{outcomes['no_match']} no_match, "
                f"{outcomes['fpcalc_failed']} fpcalc_failed"
            ),
        ))
    finally:
        await acoustid.aclose()
        await musicbrainz.aclose()
        await songlink.aclose()
        await state.bus.flush()
    return outcomes


async def ai_scan_unmatched(
    state: "AppState", *, batch_size: int = 20, limit: int = 100000,
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
        await state.bus.publish(ProgressEvent(
            stage="ai_scan", processed=0, total=0,
            message=f"no {status} files for AI scan",
        ))
        return {"updated": 0, **by_confidence}
    files = [
        {"id": r[0], "path": r[1], "artist": r[2], "title": r[3], "album": r[4]}
        for r in rows
    ]
    total = len(files)
    updated = 0
    processed = 0
    await state.bus.publish(ProgressEvent(
        stage="ai_scan", processed=0, total=total,
        message=f"sending {total} {status} files to Claude in batches of {batch_size}",
    ))
    ai = AIClient()
    try:
        for i in range(0, total, batch_size):
            if state.cancel_event.is_set():
                await state.bus.publish(ProgressEvent(
                    stage="ai_scan", processed=processed, total=total,
                    message="cancelled",
                ))
                return {"updated": updated, **by_confidence}
            batch = files[i : i + batch_size]
            ids = [f["id"] for f in batch]
            placeholders = ",".join("?" * len(ids))
            cur2 = await state.db_conn.execute(
                f"SELECT id FROM local_file "
                f"WHERE id IN ({placeholders}) AND status=?",
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
                await state.bus.publish(ProgressEvent(
                    stage="ai_scan", processed=processed, total=total,
                    message=f"failed: {e}",
                ))
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
            await state.bus.publish(ProgressEvent(
                stage="ai_scan", processed=processed, total=total,
                message=(
                    f"updated {updated}, "
                    f"high {by_confidence['high']} / "
                    f"medium {by_confidence['medium']} / "
                    f"low {by_confidence['low']} / "
                    f"none {by_confidence['none']}"
                ),
            ))
        await state.bus.publish(ProgressEvent(
            stage="ai_scan", processed=total, total=total,
            message=f"done — {updated} files have AI metadata",
        ))
    finally:
        await ai.aclose()
        await state.bus.flush()
    return {"updated": updated, **by_confidence}


async def count_unmatched(conn) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM local_file WHERE status='unmatched'"
    )
    (n,) = await cur.fetchone()
    return n
