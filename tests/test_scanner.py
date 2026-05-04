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
