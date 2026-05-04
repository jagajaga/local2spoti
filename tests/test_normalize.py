from local2spoti.normalize import normalize_artist, normalize_title, similarity, alpha_bucket


def test_strip_feat():
    assert normalize_artist("Daft Punk feat. Pharrell") == "daft punk"
    assert normalize_artist("Jay-Z ft. Kanye West") == "jay-z"
    assert normalize_artist("Adele featuring Beyoncé") == "adele"


def test_strip_feat_in_title():
    assert normalize_title("Get Lucky (feat. Pharrell Williams)") == "get lucky"
    assert normalize_title("Otis (ft. Otis Redding)") == "otis"


def test_preserve_version_qualifiers():
    assert normalize_title("Yesterday (Remastered 2009)") == "yesterday (remastered 2009)"
    assert normalize_title("Live and Let Die - Live") == "live and let die - live"


def test_unicode_nfc_lowercase():
    assert normalize_title("Café") == "café"
    assert normalize_artist("BJÖRK") == "björk"


def test_similarity_exact():
    assert similarity("Daft Punk", "daft punk") == 1.0


def test_similarity_close():
    assert 0.85 < similarity("The Beatles", "Beatles") < 1.0


def test_similarity_unrelated():
    assert similarity("Daft Punk", "Metallica") < 0.4


def test_alpha_bucket():
    assert alpha_bucket("AC/DC") == "A"
    assert alpha_bucket("björk") == "B"
    assert alpha_bucket("123 Fake") == "#"
    assert alpha_bucket("") == "#"
