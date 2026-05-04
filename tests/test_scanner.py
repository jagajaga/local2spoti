from pathlib import Path
from local2spoti.scanner import parse_filename


def test_artist_dash_title():
    a, t, n = parse_filename("Daft Punk - Around the World.mp3", parents=("Music",))
    assert (a, t, n) == ("Daft Punk", "Around the World", None)


def test_track_artist_title():
    a, t, n = parse_filename("01 - Daft Punk - Around the World.mp3", parents=("Music",))
    assert (a, t, n) == ("Daft Punk", "Around the World", 1)


def test_track_title_uses_folder_artist():
    a, t, n = parse_filename("05. Around the World.flac", parents=("Daft Punk",))
    assert (a, t, n) == ("Daft Punk", "Around the World", 5)


def test_track_dot_title():
    a, t, n = parse_filename("12. Title.mp3", parents=("Artist",))
    assert (a, t, n) == ("Artist", "Title", 12)


def test_unparseable_returns_nones():
    a, t, n = parse_filename("track.mp3", parents=())
    assert (a, t, n) == (None, None, None)


def test_unicode_filename():
    a, t, _ = parse_filename("Björk - Hyperballad.flac", parents=())
    assert a == "Björk"
    assert t == "Hyperballad"


import pytest
from local2spoti.scanner import read_tags, ParsedMetadata


def test_read_tags_mp3(make_mp3):
    p = make_mp3("track.mp3")
    md = read_tags(p)
    assert md.artist == "Daft Punk"
    assert md.title == "Around the World"
    assert md.album == "Homework"
    assert md.duration_ms is not None and md.duration_ms > 0


def test_read_tags_missing_returns_none_fields(tmp_path):
    p = tmp_path / "empty.mp3"
    p.write_bytes(b"\x00" * 16)  # not a real mp3
    md = read_tags(p)
    assert md.artist is None
    assert md.title is None


from local2spoti.scanner import walk_audio_files


def test_walk_finds_audio_files(tmp_path):
    (tmp_path / "Daft Punk").mkdir()
    (tmp_path / "Daft Punk" / "01 - Track.mp3").touch()
    (tmp_path / "Daft Punk" / "02 - Track.flac").touch()
    (tmp_path / "Daft Punk" / "cover.jpg").touch()
    (tmp_path / "notes.txt").touch()

    out = sorted(p.name for p, _ in walk_audio_files(tmp_path))
    assert out == ["01 - Track.mp3", "02 - Track.flac"]


def test_walk_returns_parents(tmp_path):
    (tmp_path / "A" / "B").mkdir(parents=True)
    f = tmp_path / "A" / "B" / "song.mp3"
    f.touch()
    [(_, parents)] = list(walk_audio_files(tmp_path))
    assert parents == ("B", "A")
