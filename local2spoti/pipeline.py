from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from . import repo
from .artist_match import FileMatchResult, match_artist_group, match_per_track
from .events import EventBus, ProgressEvent
from .matcher import Threshold
from .models import FileStatus, LocalFile, MetadataSource
from .normalize import normalize_artist
from .scanner import parse_filename, read_tags, walk_audio_files
from .spotify_client import SpotifyClient


@dataclass(slots=True)
class ScanResult:
    processed_files: int
    matched: int
    review: int
    unmatched: int
    errors: int


async def _stage_discovery(
    conn: aiosqlite.Connection, library_root: Path, *, now: datetime,
    bus: EventBus,
) -> int:
    changed = 0
    seen = 0
    for path, parents in walk_audio_files(library_root):
        seen += 1
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        f = LocalFile(
            path=str(path), mtime=int(st.st_mtime), size=st.st_size,
            format=path.suffix.lower().lstrip("."),
        )
        if await repo.upsert_local_file(conn, f, now=now):
            changed += 1
        await repo.touch_last_scanned(conn, str(path), now)
        if seen % 200 == 0:
            await bus.publish(ProgressEvent(stage="discovery", processed=seen, total=seen))
    await bus.publish(ProgressEvent(stage="discovery", processed=seen, total=seen))
    await repo.mark_missing_files(conn, scan_started=now)
    return changed


async def _stage_metadata(conn: aiosqlite.Connection, *, bus: EventBus) -> None:
    cur = await conn.execute("SELECT id, path FROM local_file WHERE status='new'")
    rows = await cur.fetchall()
    total = len(rows)
    if total == 0:
        return
    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(16)
    processed = 0

    async def process(file_id: int, path_str: str) -> None:
        nonlocal processed
        async with sem:
            path = Path(path_str)
            md = await loop.run_in_executor(None, read_tags, path)
            if not md.artist or not md.title:
                a, t, n = parse_filename(
                    path.name,
                    parents=tuple(p.name for p in path.parents[:-1]),
                )
                if a and t:
                    md.artist = md.artist or a
                    md.title = md.title or t
                    md.track_number = md.track_number or n
                    source = MetadataSource.FILENAME.value
                else:
                    await conn.execute(
                        "UPDATE local_file SET status='unmatched', metadata_source='none' WHERE id=?",
                        (file_id,),
                    )
                    await conn.commit()
                    processed += 1
                    return
            else:
                source = MetadataSource.TAGS.value
            await conn.execute(
                """UPDATE local_file SET artist=?, title=?, album=?, track_number=?,
                   duration_ms=?, metadata_source=?, status='scanned' WHERE id=?""",
                (md.artist, md.title, md.album, md.track_number, md.duration_ms,
                 source, file_id),
            )
            await conn.commit()
            processed += 1
            if processed % 50 == 0:
                await bus.publish(ProgressEvent(stage="metadata", processed=processed, total=total))

    await asyncio.gather(*[process(r[0], r[1]) for r in rows])
    await bus.publish(ProgressEvent(stage="metadata", processed=total, total=total))


async def _stage_match(
    conn: aiosqlite.Connection, client: SpotifyClient, threshold: Threshold,
    *, bus: EventBus, now: datetime,
) -> dict[str, int]:
    cur = await conn.execute(
        "SELECT id, path, artist, title, album, duration_ms FROM local_file WHERE status='scanned'"
    )
    rows = await cur.fetchall()
    if not rows:
        return {"matched": 0, "review": 0, "unmatched": 0}
    groups: dict[str, list[LocalFile]] = defaultdict(list)
    for r in rows:
        f = LocalFile(
            id=r[0], path=r[1], mtime=0, size=0, format="",
            artist=r[2], title=r[3], album=r[4], duration_ms=r[5],
            status=FileStatus.SCANNED,
        )
        groups[normalize_artist(r[2] or "")].append(f)

    counts = {"matched": 0, "review": 0, "unmatched": 0}
    total = len(rows)
    processed = 0
    sem = asyncio.Semaphore(12)

    async def process_artist(_: str, files: list[LocalFile]) -> None:
        nonlocal processed
        async with sem:
            results = await match_artist_group(
                client=client, artist=files[0].artist or "", files=files,
                threshold=threshold,
            )
            no_artist_files = [r.file for r in results if r.decision == "no_artist"]
            if no_artist_files:
                fallbacks = await match_per_track(
                    client=client, files=no_artist_files, threshold=threshold,
                )
                results = [r for r in results if r.decision != "no_artist"] + fallbacks
            await _persist_matches(conn, results, now=now, counts=counts)
            processed += len(files)
            await bus.publish(ProgressEvent(
                stage="match", processed=processed, total=total,
                matched=counts["matched"], review=counts["review"], unmatched=counts["unmatched"],
            ))

    await asyncio.gather(*[process_artist(a, fs) for a, fs in groups.items()])
    await bus.publish(ProgressEvent(
        stage="match", processed=total, total=total,
        matched=counts["matched"], review=counts["review"], unmatched=counts["unmatched"],
    ))
    return counts


async def _persist_matches(
    conn: aiosqlite.Connection, results: list[FileMatchResult], *,
    now: datetime, counts: dict[str, int],
) -> None:
    for r in results:
        assert r.file.id is not None
        if r.decision == "auto" and r.top_candidate:
            await repo.update_match(
                conn, r.file.id,
                spotify_track_id=r.top_candidate.spotify_track_id,
                confidence=r.top_candidate.confidence,
                method="auto",
            )
            counts["matched"] += 1
        elif r.decision == "review" and r.candidates:
            await repo.set_status(conn, r.file.id, FileStatus.REVIEW)
            await repo.insert_candidates(conn, r.file.id, r.candidates, now=now)
            counts["review"] += 1
        else:
            await repo.set_status(conn, r.file.id, FileStatus.UNMATCHED)
            counts["unmatched"] += 1


async def run_scan(
    *,
    conn: aiosqlite.Connection,
    client: SpotifyClient,
    library_root: Path,
    threshold: Threshold,
    bus: EventBus,
) -> ScanResult:
    now = datetime.now(UTC)
    cur = await conn.execute(
        """INSERT INTO scan_run (root_path, started_at, status, threshold)
           VALUES (?, ?, 'running', ?)""",
        (str(library_root), now.isoformat(), threshold.value),
    )
    run_id = cur.lastrowid
    await conn.commit()

    try:
        changed = await _stage_discovery(conn, library_root, now=now, bus=bus)
        await _stage_metadata(conn, bus=bus)
        counts = await _stage_match(conn, client, threshold, bus=bus, now=now)

        await conn.execute(
            """UPDATE scan_run SET finished_at=?, status='completed',
               total_files=?, matched_count=?, review_count=?, unmatched_count=?
               WHERE id=?""",
            (datetime.now(UTC).isoformat(), changed,
             counts["matched"], counts["review"], counts["unmatched"], run_id),
        )
        await conn.commit()
        await bus.flush()
        return ScanResult(
            processed_files=changed,
            matched=counts["matched"], review=counts["review"],
            unmatched=counts["unmatched"], errors=0,
        )
    except Exception as e:
        await conn.execute(
            "UPDATE scan_run SET status='failed', error_message=? WHERE id=?",
            (str(e)[:500], run_id),
        )
        await conn.commit()
        raise
