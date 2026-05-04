from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import mutagen

AUDIO_EXTS = {".mp3", ".flac", ".aac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".wma"}

_PAT_TRACK_ARTIST_TITLE = re.compile(r"^\s*(\d{1,3})\s*[-_.]\s*(.+?)\s*-\s*(.+)$")
_PAT_ARTIST_TITLE = re.compile(r"^(.+?)\s*-\s*(.+)$")
_PAT_TRACK_TITLE = re.compile(r"^\s*(\d{1,3})[\s.\-_]+(.+)$")


def parse_filename(
    filename: str, *, parents: tuple[str, ...] = (),
) -> tuple[str | None, str | None, int | None]:
    """Parse `filename` (basename) into (artist, title, track_number).

    Tries patterns in order:
      1. "01 - Artist - Title.ext"
      2. "Artist - Title.ext"
      3. "01 - Title.ext"  (artist comes from parents[0])
      4. "01. Title.ext"   (artist comes from parents[0])
    """
    stem = Path(filename).stem

    m = _PAT_TRACK_ARTIST_TITLE.match(stem)
    if m:
        return m.group(2).strip(), m.group(3).strip(), int(m.group(1))

    m = _PAT_ARTIST_TITLE.match(stem)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None

    m = _PAT_TRACK_TITLE.match(stem)
    if m and parents:
        return parents[0], m.group(2).strip(), int(m.group(1))

    return None, None, None


@dataclass(slots=True)
class ParsedMetadata:
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    track_number: int | None = None
    duration_ms: int | None = None


def _first(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _parse_track_no(value: object) -> int | None:
    s = _first(value)
    if not s:
        return None
    head = s.split("/")[0].strip()
    return int(head) if head.isdigit() else None


def read_tags(path: Path) -> ParsedMetadata:
    try:
        f = mutagen.File(str(path), easy=True)
    except Exception:
        return ParsedMetadata()
    if f is None:
        return ParsedMetadata()
    tags = dict(f.tags) if f.tags else {}
    duration_ms: int | None = None
    if f.info and getattr(f.info, "length", None):
        duration_ms = int(f.info.length * 1000)
    return ParsedMetadata(
        artist=_first(tags.get("artist")),
        title=_first(tags.get("title")),
        album=_first(tags.get("album")),
        track_number=_parse_track_no(tags.get("tracknumber")),
        duration_ms=duration_ms,
    )


def _is_hidden(name: str) -> bool:
    """True for any name we should skip during a scan.

    Covers:
      - dotfiles (.DS_Store, .Trashes, .Spotlight-V100, .fseventsd)
      - macOS AppleDouble metadata sidecars (._SomeFile.mp3) — these appear
        on non-HFS volumes (ExFAT/NTFS external drives) and have the same
        extension as the real file but are tiny resource-fork stubs.
    """
    return name.startswith(".")


def walk_audio_files(root: Path) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """Yield (file_path, parent_folders) tuples for all audio files under root.

    `parent_folders` is ordered from immediate parent outward, used by parse_filename.
    Uses os.scandir for speed at large library sizes. Hidden files and directories
    (dotfiles, AppleDouble `._*` files) are skipped.
    """
    def _walk(d: Path, parents: tuple[str, ...]) -> Iterator[tuple[Path, tuple[str, ...]]]:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for entry in entries:
            if _is_hidden(entry.name):
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(Path(entry.path), (entry.name, *parents))
            else:
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in AUDIO_EXTS:
                    yield Path(entry.path), parents

    yield from _walk(root, ())
