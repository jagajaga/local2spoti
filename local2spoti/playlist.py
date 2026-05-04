from __future__ import annotations

from dataclasses import dataclass

from .normalize import alpha_bucket


@dataclass(slots=True)
class PlaylistChunkPlan:
    chunk_index: int
    alpha_range: str
    name: str
    track_ids: list[str]


def chunk_files_alpha(
    files: list[dict],
    *,
    chunk_size: int = 9000,
) -> list[PlaylistChunkPlan]:
    sorted_files = sorted(files, key=lambda f: (f.get("artist") or "").lower())
    chunks: list[PlaylistChunkPlan] = []
    buffer: list[dict] = []
    for f in sorted_files:
        buffer.append(f)
        if len(buffer) >= chunk_size:
            chunks.append(_buffer_to_chunk(buffer, chunk_index=len(chunks) + 1))
            buffer = []
    if buffer:
        chunks.append(_buffer_to_chunk(buffer, chunk_index=len(chunks) + 1))

    total = len(chunks)
    for c in chunks:
        c.name = f"Local Library {c.chunk_index}/{total} ({c.alpha_range})"
    return chunks


def _buffer_to_chunk(buffer: list[dict], *, chunk_index: int) -> PlaylistChunkPlan:
    first = alpha_bucket(buffer[0]["artist"] or "")
    last = alpha_bucket(buffer[-1]["artist"] or "")
    alpha_range = first if first == last else f"{first}-{last}"
    return PlaylistChunkPlan(
        chunk_index=chunk_index,
        alpha_range=alpha_range,
        name=f"Local Library {chunk_index} ({alpha_range})",
        track_ids=[f["spotify_track_id"] for f in buffer],
    )
