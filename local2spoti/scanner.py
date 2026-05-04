from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

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
