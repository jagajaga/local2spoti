from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _make_silent_mp3(path: Path, *, artist: str, title: str, album: str = "Album") -> None:
    if _ffmpeg() is None:
        pytest.skip("ffmpeg required to generate audio fixtures")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1", "-q:a", "9",
            "-metadata", f"artist={artist}",
            "-metadata", f"title={title}",
            "-metadata", f"album={album}",
            str(path),
        ],
        check=True, capture_output=True,
    )


@pytest.fixture
def make_mp3(tmp_path):
    def _make(name: str, *, artist: str = "Daft Punk",
              title: str = "Around the World", album: str = "Homework") -> Path:
        p = tmp_path / name
        _make_silent_mp3(p, artist=artist, title=title, album=album)
        return p
    return _make
