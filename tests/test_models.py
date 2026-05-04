from local2spoti.models import LocalFile, MatchCandidate, FileStatus


def test_local_file_defaults():
    f = LocalFile(path="/x.mp3", mtime=1, size=2, format="mp3")
    assert f.status == FileStatus.NEW
    assert f.metadata_source is None


def test_status_transitions_valid():
    assert FileStatus.NEW.value == "new"
    assert FileStatus.MATCHED.value == "matched"


def test_candidate_score_ordering():
    a = MatchCandidate(spotify_track_id="a", spotify_artist="x",
                       spotify_title="y", artist_similarity=0.9,
                       title_similarity=0.9, confidence=0.9, rank=1)
    b = MatchCandidate(spotify_track_id="b", spotify_artist="x",
                       spotify_title="y", artist_similarity=0.5,
                       title_similarity=0.5, confidence=0.5, rank=2)
    assert sorted([b, a]) == [a, b]
