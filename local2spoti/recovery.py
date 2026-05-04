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

if TYPE_CHECKING:
    from .state import AppState


async def deep_scan_unmatched(
    state: "AppState", *, limit: int = 100000, status: str = "unmatched",
) -> dict[str, int]:
    """Run AcoustID fingerprint + lookup over every file with `status=<status>`.

    Default operates on 'unmatched' files (no Spotify match found). Pass
    status='review' to re-fingerprint files in the review queue, replacing
    their stale tags with whatever AcoustID identifies — useful when the
    review candidates look obviously wrong.

    Returns the per-outcome counts. Emits stage='deep_scan' progress events.
    Cancels cleanly on `state.cancel_event`. Aborts the whole run on the
    first AcoustID auth/quota error.
    """
    cur = await state.db_conn.execute(
        "SELECT id, path, duration_ms FROM local_file WHERE status=? LIMIT ?",
        (status, limit),
    )
    rows = await cur.fetchall()
    outcomes = {"matched": 0, "no_match": 0, "fpcalc_failed": 0}
    if not rows:
        await state.bus.publish(ProgressEvent(
            stage="deep_scan", processed=0, total=0,
            message=f"no {status} files to deep-scan",
        ))
        return outcomes
    total = len(rows)
    processed = 0
    await state.bus.publish(ProgressEvent(
        stage="deep_scan", processed=0, total=total,
        message=f"fingerprinting {total} {status} files",
    ))
    acoustid = AcoustidClient(api_key=state.settings.acoustid_api_key)
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
            if row2 is None or row2[0] != status:
                processed += 1
                continue
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
                    await repo.clear_candidates(state.db_conn, fid)
                    await state.db_conn.execute(
                        """UPDATE local_file SET artist=?, title=?, status='scanned',
                           metadata_source='acoustid' WHERE id=?""",
                        (md.artist, md.title, fid),
                    )
                    await state.db_conn.commit()
                    outcomes["matched"] += 1
            processed += 1
            await state.bus.publish(ProgressEvent(
                stage="deep_scan", processed=processed, total=total,
                message=(
                    f"matched {outcomes['matched']} / "
                    f"no_match {outcomes['no_match']} / "
                    f"fpcalc_failed {outcomes['fpcalc_failed']}"
                ),
            ))
        await state.bus.publish(ProgressEvent(
            stage="deep_scan", processed=total, total=total,
            message=(
                f"done — matched {outcomes['matched']}, "
                f"no_match {outcomes['no_match']}, "
                f"fpcalc_failed {outcomes['fpcalc_failed']}"
            ),
        ))
    finally:
        await acoustid.aclose()
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
