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


def test_walk_skips_appledouble_files(tmp_path):
    """._*.mp3 sidecars (macOS AppleDouble) on non-HFS volumes must be skipped."""
    (tmp_path / "Album").mkdir()
    (tmp_path / "Album" / "Real Track.mp3").touch()
    (tmp_path / "Album" / "._Real Track.mp3").touch()
    (tmp_path / "Album" / "._Other.flac").touch()
    (tmp_path / "._Top Level.mp3").touch()

    from local2spoti.scanner import walk_audio_files
    out = sorted(p.name for p, _ in walk_audio_files(tmp_path))
    assert out == ["Real Track.mp3"]


def test_walk_skips_dot_directories(tmp_path):
    """Dot-prefixed directories (.Trashes, .Spotlight-V100, .fseventsd) must be skipped."""
    (tmp_path / "Music").mkdir()
    (tmp_path / "Music" / "song.mp3").touch()
    (tmp_path / ".Trashes").mkdir()
    (tmp_path / ".Trashes" / "junk.mp3").touch()
    (tmp_path / ".Spotlight-V100").mkdir()
    (tmp_path / ".Spotlight-V100" / "x.mp3").touch()

    from local2spoti.scanner import walk_audio_files
    out = sorted(p.name for p, _ in walk_audio_files(tmp_path))
    assert out == ["song.mp3"]


def test_walk_skips_ds_store_and_iTunes_metadata(tmp_path):
    """Both .DS_Store and .iTunes-*.plist style names get filtered."""
    (tmp_path / "track.mp3").touch()
    (tmp_path / ".DS_Store").touch()
    (tmp_path / "._.iTunes Preferences.plist").touch()
    (tmp_path / "._iTunes Library.itl").touch()

    from local2spoti.scanner import walk_audio_files
    out = sorted(p.name for p, _ in walk_audio_files(tmp_path))
    assert out == ["track.mp3"]


def test_is_hidden_classifier():
    """Pure unit: covers every prefix we need to filter."""
    from local2spoti.scanner import _is_hidden
    assert _is_hidden(".DS_Store")
    assert _is_hidden("._Track.mp3")
    assert _is_hidden(".Spotlight-V100")
    assert _is_hidden(".Trashes")
    assert _is_hidden(".fseventsd")
    assert not _is_hidden("Track.mp3")
    assert not _is_hidden("Album Cover.jpg")
    assert not _is_hidden("01 - Song.flac")
