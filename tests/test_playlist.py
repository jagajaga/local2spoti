from local2spoti.playlist import chunk_files_alpha


def _files(*artists):
    return [{"artist": a, "spotify_track_id": f"t{i}"} for i, a in enumerate(artists)]


def test_single_chunk_under_capacity():
    files = _files(*[f"Artist{i}" for i in range(50)])
    chunks = chunk_files_alpha(files, chunk_size=9000)
    assert len(chunks) == 1
    assert chunks[0].alpha_range.startswith("A")
    assert len(chunks[0].track_ids) == 50


def test_alpha_split_when_over_capacity():
    artists = (
        [f"A{i:04d}" for i in range(5000)]
        + [f"M{i:04d}" for i in range(5000)]
        + [f"Z{i:04d}" for i in range(2000)]
    )
    files = _files(*artists)
    chunks = chunk_files_alpha(files, chunk_size=9000)
    assert len(chunks) >= 2
    assert sum(len(c.track_ids) for c in chunks) == 12000
    for c in chunks:
        assert c.alpha_range


def test_chunk_index_starts_at_one():
    chunks = chunk_files_alpha(_files("A", "B"), chunk_size=9000)
    assert chunks[0].chunk_index == 1


def test_chunk_name_contains_index_and_total():
    files = _files(*[f"A{i:04d}" for i in range(10000)] + [f"Z{i:04d}" for i in range(2000)])
    chunks = chunk_files_alpha(files, chunk_size=9000)
    names = [c.name for c in chunks]
    for i, n in enumerate(names, start=1):
        assert f"{i}/{len(chunks)}" in n
