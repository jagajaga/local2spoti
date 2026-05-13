from local2spoti.matcher import Threshold, decide, score_candidate


def test_score_perfect_match():
    s = score_candidate(
        local_artist="Daft Punk",
        local_title="Around the World",
        local_album="Homework",
        local_duration_ms=423000,
        spotify_artist="Daft Punk",
        spotify_title="Around the World",
        spotify_album="Homework",
        spotify_duration_ms=423500,
    )
    assert s.confidence > 0.95
    assert s.artist_similarity == 1.0
    assert s.title_similarity == 1.0


def test_score_typo_artist():
    s = score_candidate(
        local_artist="Daft Pnk",
        local_title="Around the World",
        local_album=None,
        local_duration_ms=None,
        spotify_artist="Daft Punk",
        spotify_title="Around the World",
        spotify_album=None,
        spotify_duration_ms=None,
    )
    assert 0.7 < s.confidence < 0.95


def test_score_unrelated():
    s = score_candidate(
        local_artist="Daft Punk",
        local_title="Around the World",
        local_album=None,
        local_duration_ms=None,
        spotify_artist="Metallica",
        spotify_title="Battery",
        spotify_album=None,
        spotify_duration_ms=None,
    )
    assert s.confidence < 0.4


def test_decide_balanced_auto_match():
    assert (
        decide(
            artist_sim=0.95,
            title_sim=0.95,
            album_match=True,
            duration_delta_ms=1000,
            threshold=Threshold.BALANCED,
        )
        == "auto"
    )


def test_decide_strict_demands_high_sim():
    assert (
        decide(
            artist_sim=0.92,
            title_sim=0.92,
            album_match=True,
            duration_delta_ms=1000,
            threshold=Threshold.STRICT,
        )
        == "review"
    )


def test_decide_loose():
    assert (
        decide(
            artist_sim=0.85,
            title_sim=0.82,
            album_match=False,
            duration_delta_ms=None,
            threshold=Threshold.LOOSE,
        )
        == "auto"
    )


def test_decide_balanced_album_match_alone_is_not_auto():
    """Regression: BALANCED used to auto-promote when album_match was
    True even with the duration off (or absent). Now duration must be
    within 5s — album_match alone is no longer a sufficient safety net.
    """
    assert (
        decide(
            artist_sim=0.95,
            title_sim=0.95,
            album_match=True,
            duration_delta_ms=15000,  # 15s off — outside the 5s window
            threshold=Threshold.BALANCED,
        )
        == "review"
    )
    # And with no duration info at all, also review (was previously auto
    # via the album_match path).
    assert (
        decide(
            artist_sim=0.95,
            title_sim=0.95,
            album_match=True,
            duration_delta_ms=None,
            threshold=Threshold.BALANCED,
        )
        == "review"
    )


def test_score_flags_variant_mismatch():
    """Spotify's title says 'Live'; local says 'Mony Mony'. The
    candidate should be flagged variant_mismatch and have its
    confidence penalized so the clean studio cut outranks it.
    """
    from local2spoti.matcher import score_candidate

    studio = score_candidate(
        local_artist="Billy Idol",
        local_title="Mony Mony",
        local_album=None,
        local_duration_ms=200000,
        spotify_artist="Billy Idol",
        spotify_title="Mony Mony",
        spotify_album=None,
        spotify_duration_ms=200000,
    )
    live = score_candidate(
        local_artist="Billy Idol",
        local_title="Mony Mony",
        local_album=None,
        local_duration_ms=200000,
        spotify_artist="Billy Idol",
        spotify_title="Mony Mony - Live at MSG",
        spotify_album=None,
        spotify_duration_ms=200000,
    )
    assert studio.variant_mismatch is False
    assert live.variant_mismatch is True
    # Studio confidence must rank above live, even though they share
    # the same artist/title/duration.
    assert studio.confidence > live.confidence


def test_decide_refuses_auto_for_variant_mismatch():
    """Even with perfect artist/title sim and matching duration, a
    live/remix/remaster candidate must go to review (not auto)."""
    assert (
        decide(
            artist_sim=1.0,
            title_sim=0.92,  # 'Mony Mony - Live at MSG' vs 'Mony Mony'
            album_match=False,
            duration_delta_ms=500,
            threshold=Threshold.BALANCED,
            variant_mismatch=True,
        )
        == "review"
    )


def test_decide_unmatched_when_low():
    assert (
        decide(
            artist_sim=0.3,
            title_sim=0.3,
            album_match=False,
            duration_delta_ms=None,
            threshold=Threshold.BALANCED,
        )
        == "unmatched"
    )
