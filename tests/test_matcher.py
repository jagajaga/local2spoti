from local2spoti.matcher import score_candidate, decide, Threshold


def test_score_perfect_match():
    s = score_candidate(
        local_artist="Daft Punk", local_title="Around the World", local_album="Homework",
        local_duration_ms=423000,
        spotify_artist="Daft Punk", spotify_title="Around the World",
        spotify_album="Homework", spotify_duration_ms=423500,
    )
    assert s.confidence > 0.95
    assert s.artist_similarity == 1.0
    assert s.title_similarity == 1.0


def test_score_typo_artist():
    s = score_candidate(
        local_artist="Daft Pnk", local_title="Around the World", local_album=None,
        local_duration_ms=None,
        spotify_artist="Daft Punk", spotify_title="Around the World",
        spotify_album=None, spotify_duration_ms=None,
    )
    assert 0.7 < s.confidence < 0.95


def test_score_unrelated():
    s = score_candidate(
        local_artist="Daft Punk", local_title="Around the World", local_album=None,
        local_duration_ms=None,
        spotify_artist="Metallica", spotify_title="Battery",
        spotify_album=None, spotify_duration_ms=None,
    )
    assert s.confidence < 0.4


def test_decide_balanced_auto_match():
    assert decide(artist_sim=0.95, title_sim=0.95, album_match=True,
                  duration_delta_ms=1000, threshold=Threshold.BALANCED) == "auto"


def test_decide_strict_demands_high_sim():
    assert decide(artist_sim=0.92, title_sim=0.92, album_match=True,
                  duration_delta_ms=1000, threshold=Threshold.STRICT) == "review"


def test_decide_loose():
    assert decide(artist_sim=0.85, title_sim=0.82, album_match=False,
                  duration_delta_ms=None, threshold=Threshold.LOOSE) == "auto"


def test_decide_unmatched_when_low():
    assert decide(artist_sim=0.3, title_sim=0.3, album_match=False,
                  duration_delta_ms=None, threshold=Threshold.BALANCED) == "unmatched"
