from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4

AUDIO_EXTS = {".mp3", ".flac", ".aac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".wma"}

# Register ISRC handler so EasyID3 surfaces TSRC via the unified `isrc`
# key. Vorbis (FLAC/Ogg/Opus) already exposes 'isrc' through the easy
# interface natively, so it works without registration. MP4/M4A is the
# odd one out: EasyMP4 has no freeform-atom helper, so we read the
# `----:com.apple.iTunes:ISRC` atom directly when the easy read returned
# nothing (see read_tags).
try:
    EasyID3.RegisterTextKey("isrc", "TSRC")
except (KeyError, ValueError):
    # Already registered (re-import in tests, etc.) — the call raises if
    # the key collides with an existing mapping; idempotency-safe to ignore.
    pass

_PAT_TRACK_ARTIST_TITLE = re.compile(r"^\s*(\d{1,3})\s*[-_.]\s*(.+?)\s*-\s*(.+)$")
_PAT_ARTIST_TITLE = re.compile(r"^(.+?)\s*-\s*(.+)$")
_PAT_TRACK_TITLE = re.compile(r"^\s*(\d{1,3})[\s.\-_]+(.+)$")


def parse_filename(
    filename: str,
    *,
    parents: tuple[str, ...] = (),
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
    isrc: str | None = None


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


# ISRC canonical form is 12 alphanumerics: CC (country) + XXX (registrant)
# + YY (year) + NNNNN (designation). Tags in the wild often arrive with
# dashes ("US-RC1-12-34567") or padding whitespace; strip them and reject
# anything that doesn't match the spec so we don't fire bogus queries.
def _parse_isrc(value: object) -> str | None:
    s = _first(value)
    if not s:
        return None
    cleaned = "".join(ch for ch in s if ch.isalnum()).upper()
    if len(cleaned) != 12 or not cleaned.isalnum():
        return None
    return cleaned


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
    isrc = _parse_isrc(tags.get("isrc"))
    if isrc is None and path.suffix.lower() in (".m4a", ".mp4", ".aac"):
        isrc = _read_mp4_isrc(path)
    return ParsedMetadata(
        artist=_first(tags.get("artist")),
        title=_first(tags.get("title")),
        album=_first(tags.get("album")),
        track_number=_parse_track_no(tags.get("tracknumber")),
        duration_ms=duration_ms,
        isrc=isrc,
    )


def _read_mp4_isrc(path: Path) -> str | None:
    """MP4/iTunes stashes ISRC in a freeform atom. EasyMP4 can't see it
    without per-key registration, so when the easy interface didn't
    surface one, fall through to a direct MP4 read."""
    try:
        mp4 = MP4(str(path))
    except Exception:
        return None
    if mp4.tags is None:
        return None
    raw = mp4.tags.get("----:com.apple.iTunes:ISRC")
    if not raw:
        return None
    # MP4Freeform values are bytes; decode-or-skip without crashing on
    # mis-tagged bytes that aren't actually UTF-8.
    val = raw[0]
    if isinstance(val, bytes):
        try:
            val = val.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return _parse_isrc(val)


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
