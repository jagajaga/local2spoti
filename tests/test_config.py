from pathlib import Path
from local2spoti.config import Settings, load_settings


def test_default_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = load_settings()
    assert s.data_dir == tmp_path / ".local2spoti"
    assert s.db_path == s.data_dir / "state.db"
    assert s.log_dir == s.data_dir / "logs"


def test_threshold_default():
    s = Settings(spotify_client_id="abc")
    assert s.threshold == "balanced"
    assert s.host == "127.0.0.1"
    assert s.port == 8000


def test_creates_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = load_settings()
    s.ensure_dirs()
    assert s.data_dir.is_dir()
    assert s.log_dir.is_dir()
