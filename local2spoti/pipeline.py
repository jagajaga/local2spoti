from __future__ import annotations

import asyncio
import contextlib
import time
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
    conn: aiosqlite.Connection,
    library_root: Path,
    *,
    now: datetime,
    bus: EventBus,
) -> int:
    changed = 0
    seen = 0
    for path, _parents in walk_audio_files(library_root):
        seen += 1
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        f = LocalFile(
            path=str(path),
            mtime=int(st.st_mtime),
            size=st.st_size,
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
        # Without an event the metadata bar stays "idle" forever even
        # though the stage actually ran (incremental rescan with no new
        # files is the common case, so this isn't an error).
        await bus.publish(
            ProgressEvent(
                stage="metadata",
                processed=0,
                total=0,
                message="nothing to extract — no new files since last scan",
            )
        )
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
                   duration_ms=?, isrc=?, metadata_source=?, status='scanned' WHERE id=?""",
                (
                    md.artist,
                    md.title,
                    md.album,
                    md.track_number,
                    md.duration_ms,
                    md.isrc,
                    source,
                    file_id,
                ),
            )
            await conn.commit()
            processed += 1
            if processed % 50 == 0:
                await bus.publish(ProgressEvent(stage="metadata", processed=processed, total=total))

    await asyncio.gather(*[process(r[0], r[1]) for r in rows])
    await bus.publish(ProgressEvent(stage="metadata", processed=total, total=total))


async def _stage_match(
    conn: aiosqlite.Connection,
    client: SpotifyClient,
    threshold: Threshold,
    *,
    bus: EventBus,
    now: datetime,
) -> dict[str, int]:
    cur = await conn.execute(
        "SELECT id, path, artist, title, album, duration_ms, isrc FROM local_file WHERE status='scanned'"
    )
    rows = await cur.fetchall()
    if not rows:
        await bus.publish(
            ProgressEvent(
                stage="match",
                processed=0,
                total=0,
                message="nothing to match — no scanned files",
            )
        )
        return {"matched": 0, "review": 0, "unmatched": 0}
    counts = {"matched": 0, "review": 0, "unmatched": 0, "errors": 0}
    total = len(rows)

    # ISRC pre-pass: every file that came in with an ISRC tag gets a
    # single deterministic q=isrc:XXX lookup. Hits land directly on
    # status='matched' with confidence=1.0 (ISRC is a global recording
    # identifier — Spotify's index returns the exact track or nothing).
    # Misses fall through to the artist-grouped flow below. This trades
    # one /search per ISRC-having file for the otherwise-required
    # search-artist + albums-list + albums-batch chain (and avoids the
    # fuzzy review queue for tracks where we already know the answer).
    isrc_matched_ids: set[int] = set()
    isrc_files = [r for r in rows if r[6]]
    if isrc_files:
        await bus.publish(
            ProgressEvent(
                stage="match",
                processed=0,
                total=total,
                message=f"ISRC pre-pass: {len(isrc_files)} files have ISRC tags",
            )
        )
        isrc_sem = asyncio.Semaphore(8)

        async def _isrc_lookup(row) -> None:
            file_id, _path, _artist, _title, _album, _dur, isrc = row
            async with isrc_sem:
                try:
                    track = await client.search_track_by_isrc(isrc)
                except Exception:
                    # Soft fail: the file just falls through to the
                    # regular artist-grouped flow. ISRC is a fast-path,
                    # not a guarantee.
                    return
                if track is None:
                    return
                # Drop any stale candidates from a prior match run before
                # the deterministic ISRC result lands — keeps the review
                # queue from showing fuzzy candidates next to a 100%-
                # confidence match.
                await repo.clear_candidates(conn, file_id)
                await repo.update_match(
                    conn,
                    file_id,
                    spotify_track_id=track["id"],
                    confidence=1.0,
                    method="isrc",
                )
                isrc_matched_ids.add(file_id)
                counts["matched"] += 1

        await asyncio.gather(
            *[_isrc_lookup(r) for r in isrc_files],
            return_exceptions=True,
        )
        rows = [r for r in rows if r[0] not in isrc_matched_ids]
        await bus.publish(
            ProgressEvent(
                stage="match",
                processed=len(isrc_matched_ids),
                total=total,
                matched=counts["matched"],
                review=counts["review"],
                unmatched=counts["unmatched"],
                errors=counts["errors"],
                message=(
                    f"ISRC pre-pass done — {len(isrc_matched_ids)} matched "
                    f"directly, {len(isrc_files) - len(isrc_matched_ids)} "
                    f"falling through to artist match"
                ),
            )
        )

    groups: dict[str, list[LocalFile]] = defaultdict(list)
    for r in rows:
        f = LocalFile(
            id=r[0],
            path=r[1],
            mtime=0,
            size=0,
            format="",
            artist=r[2],
            title=r[3],
            album=r[4],
            duration_ms=r[5],
            isrc=r[6],
            status=FileStatus.SCANNED,
        )
        groups[normalize_artist(r[2] or "")].append(f)

    processed = len(isrc_matched_ids)
    sem = asyncio.Semaphore(12)
    current_artists: set[str] = set()
    heartbeat_stop = asyncio.Event()

    async def process_artist(_: str, files: list[LocalFile]) -> None:
        nonlocal processed
        artist_label = files[0].artist or "(unknown)"
        async with sem:
            current_artists.add(artist_label)
            try:
                # Inner try: any failure on THIS artist (Spotify 403/5xx,
                # network blip, malformed response) marks just this group's
                # files as error and lets the gather continue. Without this,
                # a single bad artist kills the whole match stage.
                try:
                    results = await match_artist_group(
                        client=client,
                        artist=files[0].artist or "",
                        files=files,
                        threshold=threshold,
                        conn=conn,
                    )
                    no_artist_files = [r.file for r in results if r.decision == "no_artist"]
                    if no_artist_files:
                        fallbacks = await match_per_track(
                            client=client,
                            files=no_artist_files,
                            threshold=threshold,
                        )
                        results = [r for r in results if r.decision != "no_artist"] + fallbacks
                    await _persist_matches(conn, results, now=now, counts=counts)
                    processed += len(files)
                    await bus.publish(
                        ProgressEvent(
                            stage="match",
                            processed=processed,
                            total=total,
                            matched=counts["matched"],
                            review=counts["review"],
                            unmatched=counts["unmatched"],
                            errors=counts["errors"],
                            message=f"matched {artist_label}",
                        )
                    )
                except Exception as exc:
                    err_msg = str(exc)[:200]
                    for f in files:
                        if f.id is not None:
                            await repo.set_status(
                                conn,
                                f.id,
                                FileStatus.ERROR,
                                last_error=err_msg,
                            )
                            counts["errors"] += 1
                    processed += len(files)
                    await bus.publish(
                        ProgressEvent(
                            stage="match",
                            processed=processed,
                            total=total,
                            matched=counts["matched"],
                            review=counts["review"],
                            unmatched=counts["unmatched"],
                            errors=counts["errors"],
                            message=f"⚠ {artist_label}: {err_msg[:80]}",
                        )
                    )
            finally:
                current_artists.discard(artist_label)

    # Heartbeat: emits every 5 sec with current activity + ETA, even when no
    # match has completed (e.g. all 12 workers blocked on Spotify rate limit
    # or one worker chewing through Beatles' deluxe-edition pagination). This
    # is what lets the user tell "stalled and dead" from "alive, working".
    heartbeat_task = asyncio.create_task(
        _match_heartbeat(
            bus=bus,
            counts=counts,
            total=total,
            get_processed=lambda: processed,
            current_artists=current_artists,
            bucket=client._bucket,  # for pause-remaining reporting
            stop=heartbeat_stop,
        )
    )

    try:
        # return_exceptions=True so even an unanticipated failure path
        # in process_artist (which we already wrap in try/except) can't
        # kill the gather. Belt-and-suspenders against future regressions.
        await asyncio.gather(
            *[process_artist(a, fs) for a, fs in groups.items()],
            return_exceptions=True,
        )
    finally:
        heartbeat_stop.set()
        with contextlib.suppress(Exception):
            await heartbeat_task

    err_suffix = f", {counts['errors']} errors" if counts["errors"] else ""
    await bus.publish(
        ProgressEvent(
            stage="match",
            processed=total,
            total=total,
            matched=counts["matched"],
            review=counts["review"],
            unmatched=counts["unmatched"],
            errors=counts["errors"],
            message=f"done — {counts['matched']} auto-matched, "
            f"{counts['review']} need review, "
            f"{counts['unmatched']} unmatched{err_suffix}",
        )
    )
    return counts


async def _match_heartbeat(
    *,
    bus: EventBus,
    counts: dict[str, int],
    total: int,
    get_processed,
    current_artists: set[str],
    bucket,  # TokenBucket; typed loosely to avoid circular import
    stop: asyncio.Event,
    interval: float = 5.0,
) -> None:
    """Fire a status event every `interval` seconds during the match stage.

    Includes throughput, ETA, and rate-limit pause status so the user can
    tell stalled-because-spotify-said-wait apart from genuinely stuck.
    """
    last_processed = 0
    last_time = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # stop was set
        except TimeoutError:
            pass
        processed = get_processed()
        now = time.monotonic()
        elapsed = now - last_time
        delta = processed - last_processed
        pause_remaining = bucket.pause_remaining()
        if delta > 0 and elapsed > 0:
            rate = delta / elapsed
            remaining = max(0, total - processed)
            eta_sec = remaining / rate if rate > 0 else None
            msg = f"~{rate:.1f}/s, ETA {_fmt_duration(eta_sec)}, {len(current_artists)} workers active"
            if pause_remaining > 0.5:
                msg += f", rate-limited {pause_remaining:.0f}s remaining"
        else:
            active = ", ".join(sorted(current_artists))[:80] or "—"
            if pause_remaining > 0.5:
                msg = (
                    f"rate-limited by Spotify — pause expires in "
                    f"{pause_remaining:.0f}s, {len(current_artists)} workers parked"
                )
            else:
                msg = f"slow tick — {len(current_artists)} workers in flight, current: {active}"
        await bus.publish(
            ProgressEvent(
                stage="match",
                processed=processed,
                total=total,
                matched=counts["matched"],
                review=counts["review"],
                unmatched=counts["unmatched"],
                message=msg,
            )
        )
        last_processed = processed
        last_time = now


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


async def _persist_matches(
    conn: aiosqlite.Connection,
    results: list[FileMatchResult],
    *,
    now: datetime,
    counts: dict[str, int],
) -> None:
    for r in results:
        assert r.file.id is not None
        # Always nuke stale candidates first — without this, a file that
        # was re-tagged between matches keeps the OLD candidates from when
        # it had a different artist/title (so a Beach Boys track ends up
        # showing Beatles candidates from a prior run).
        await repo.clear_candidates(conn, r.file.id)
        if r.decision == "auto" and r.top_candidate:
            await repo.update_match(
                conn,
                r.file.id,
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
            (
                datetime.now(UTC).isoformat(),
                changed,
                counts["matched"],
                counts["review"],
                counts["unmatched"],
                run_id,
            ),
        )
        await conn.commit()
        await bus.flush()
        return ScanResult(
            processed_files=changed,
            matched=counts["matched"],
            review=counts["review"],
            unmatched=counts["unmatched"],
            errors=0,
        )
    except Exception as e:
        await conn.execute(
            "UPDATE scan_run SET status='failed', error_message=? WHERE id=?",
            (str(e)[:500], run_id),
        )
        await conn.commit()
        raise
