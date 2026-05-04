# Local2Spoti Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local FastAPI web app that scans a folder of 15k+ audio files, matches each to Spotify via metadata search (artist-first optimization), and creates chunked Spotify playlists on the user's account.

**Architecture:** Single-process FastAPI app with async background pipeline. SQLite (WAL) holds all state for resumability and incremental rescans. uvloop event loop, bounded async/thread pools, shared token-bucket Spotify rate limiter, WebSocket-driven UI updates. Server-rendered Jinja2 + HTMX UI (no JS framework).

**Tech Stack:** Python 3.13, FastAPI, uvicorn, uvloop, aiosqlite, mutagen, spotipy (OAuth helper), httpx, orjson, rapidfuzz, structlog, Jinja2 + HTMX + Tailwind (CDN), pytest + respx + Playwright.

**Reference spec:** `docs/superpowers/specs/2026-05-04-local2spoti-design.md`

---

## File structure

```
local2spoti/
├── pyproject.toml
├── README.md
├── .gitignore
├── .github/workflows/ci.yml
├── local2spoti/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entrypoint, lifespan, uvloop
│   ├── config.py            # Settings (TOML + env), data dirs
│   ├── normalize.py         # Pure helpers: NFC, feat-strip, fold, similarity
│   ├── db.py                # SQLite connect, PRAGMAs, schema, migrations
│   ├── models.py            # Dataclasses: LocalFile, Candidate, Playlist, etc.
│   ├── repo.py              # Async DB queries (CRUD + state transitions)
│   ├── ratelimit.py         # Token-bucket limiter
│   ├── spotify_oauth.py     # PKCE flow + token refresh
│   ├── spotify_client.py    # httpx wrapper: rate limit + retries + endpoints
│   ├── scanner.py           # os.scandir walk, mutagen tags, filename fallback
│   ├── matcher.py           # Confidence scoring + threshold decisions
│   ├── artist_match.py      # Artist-first batch matching + per-track fallback
│   ├── acoustid.py          # Optional fpcalc + AcoustID client
│   ├── playlist.py          # Chunking + Spotify add-tracks
│   ├── pipeline.py          # Orchestrates stages, queues, cancellation
│   ├── events.py            # WebSocket event coalescer/broadcaster
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── ui.py            # HTMX HTML endpoints
│   │   ├── api.py           # JSON API (start/cancel scan, settings)
│   │   └── ws.py            # WebSocket /ws/progress
│   ├── templates/
│   │   ├── base.html        # Tailwind + HTMX includes
│   │   ├── dashboard.html
│   │   ├── scan.html
│   │   ├── files.html
│   │   ├── review.html
│   │   ├── unmatched.html
│   │   └── partials/        # HTMX OOB fragments
│   └── static/              # JS shortcuts, favicon
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── audio/           # Tiny generated audio files per format
    │   └── spotify/         # Captured Spotify response JSON
    ├── test_normalize.py
    ├── test_scanner.py
    ├── test_matcher.py
    ├── test_artist_match.py
    ├── test_ratelimit.py
    ├── test_playlist.py
    ├── test_pipeline.py
    ├── test_repo.py
    └── test_routes.py
```

Each file has a single responsibility. `pipeline.py` is the only file that knows about orchestration; `repo.py` is the only file that writes SQL; `spotify_client.py` is the only file that hits the network.

---

## Phase 0 — Project scaffolding

### Task 1: pyproject.toml + minimal package structure

**Files:**
- Create: `pyproject.toml`
- Create: `local2spoti/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`
- Create: `README.md`

- [ ] **Step 1: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
dist/
build/
*.egg-info/
~/.local2spoti/
state.db
state.db-wal
state.db-shm
.coverage
htmlcov/
.DS_Store
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "local2spoti"
version = "0.1.0"
description = "Local audio library to Spotify playlist sync"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "uvloop>=0.21; sys_platform != 'win32'",
    "aiosqlite>=0.20",
    "httpx>=0.28",
    "orjson>=3.10",
    "mutagen>=1.47",
    "rapidfuzz>=3.10",
    "spotipy>=2.24",
    "structlog>=24.4",
    "jinja2>=3.1",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "tomli-w>=1.0",
    "pyacoustid>=1.3",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
    "freezegun>=1.5",
    "ruff>=0.7",
    "mypy>=1.13",
    "playwright>=1.48",
    "beautifulsoup4>=4.12",
]

[project.scripts]
local2spoti = "local2spoti.main:run"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC", "SIM", "RUF"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
strict = true
python_version = "3.13"
```

- [ ] **Step 3: Write empty package init files**

`local2spoti/__init__.py`:
```python
__version__ = "0.1.0"
```

`tests/__init__.py`: (empty)

- [ ] **Step 4: Write minimal README**

`README.md`:
```markdown
# Local2Spoti

Local web app that scans a folder of audio files and creates Spotify playlists matching them.

## Setup

Requires Python 3.13, macOS or Linux.

```bash
pip install -e ".[dev]"
local2spoti
```

Then open http://127.0.0.1:8000.

See `docs/superpowers/specs/2026-05-04-local2spoti-design.md` for design details.
```

- [ ] **Step 5: Verify install**

Run: `pip install -e ".[dev]"`
Expected: installs successfully, no resolver errors.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml local2spoti/__init__.py tests/__init__.py .gitignore README.md
git commit -m "chore: project scaffolding"
```

---

### Task 2: Settings module

**Files:**
- Create: `local2spoti/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
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
```

- [ ] **Step 2: Run test (expected fail)**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with import error.

- [ ] **Step 3: Implement settings**

`local2spoti/config.py`:
```python
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Threshold = Literal["strict", "balanced", "loose"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCAL2SPOTI_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8000
    threshold: Threshold = "balanced"

    library_root: Path | None = None
    spotify_client_id: str = ""
    acoustid_api_key: str | None = None

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".local2spoti")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def config_toml(self) -> Path:
        return self.data_dir / "config.toml"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    home = Path(os.environ.get("HOME", str(Path.home())))
    data_dir = home / ".local2spoti"
    overrides: dict[str, object] = {"data_dir": data_dir}
    toml = data_dir / "config.toml"
    if toml.exists():
        with toml.open("rb") as f:
            overrides.update(tomllib.load(f))
    return Settings(**overrides)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/config.py tests/test_config.py
git commit -m "feat: settings module"
```

---

### Task 3: Normalization helpers

**Files:**
- Create: `local2spoti/normalize.py`
- Create: `tests/test_normalize.py`

- [ ] **Step 1: Write failing tests**

`tests/test_normalize.py`:
```python
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
```

- [ ] **Step 2: Run (expected fail)**

Run: `pytest tests/test_normalize.py -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement**

`local2spoti/normalize.py`:
```python
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]?",
    re.IGNORECASE,
)


def _nfc_lower(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().lower()


def normalize_artist(s: str) -> str:
    if not s:
        return ""
    return _nfc_lower(_FEAT_RE.sub("", s))


def normalize_title(s: str) -> str:
    if not s:
        return ""
    return _nfc_lower(_FEAT_RE.sub("", s))


def similarity(a: str, b: str) -> float:
    return fuzz.token_set_ratio(_nfc_lower(a), _nfc_lower(b)) / 100.0


def alpha_bucket(s: str) -> str:
    s = _nfc_lower(s).lstrip()
    if not s:
        return "#"
    first = s[0]
    return first.upper() if first.isalpha() else "#"
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_normalize.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/normalize.py tests/test_normalize.py
git commit -m "feat: normalization + similarity helpers"
```

---

### Task 4: Database schema + connection

**Files:**
- Create: `local2spoti/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

`tests/test_db.py`:
```python
import asyncio
from pathlib import Path

import pytest

from local2spoti.db import connect, init_schema


@pytest.mark.asyncio
async def test_schema_creates_all_tables(tmp_path):
    db = tmp_path / "test.db"
    async with connect(db) as conn:
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = [r[0] for r in await cur.fetchall()]
    assert rows == [
        "auth_token",
        "local_file",
        "match_candidate",
        "playlist",
        "playlist_track",
        "scan_run",
        "setting",
    ]


@pytest.mark.asyncio
async def test_pragmas_applied(tmp_path):
    db = tmp_path / "test.db"
    async with connect(db) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        cur = await conn.execute("PRAGMA foreign_keys")
        fk = (await cur.fetchone())[0]
    assert mode.lower() == "wal"
    assert fk == 1


@pytest.mark.asyncio
async def test_init_schema_idempotent(tmp_path):
    db = tmp_path / "test.db"
    async with connect(db) as conn:
        await init_schema(conn)
        await init_schema(conn)  # second run must not raise
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_db.py -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement schema + connect**

`local2spoti/db.py`:
```python
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA cache_size = -65536",
    "PRAGMA mmap_size = 268435456",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_run (
  id              INTEGER PRIMARY KEY,
  root_path       TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  status          TEXT NOT NULL,
  threshold       TEXT NOT NULL,
  total_files     INTEGER,
  matched_count   INTEGER,
  review_count    INTEGER,
  unmatched_count INTEGER,
  error_message   TEXT
);

CREATE TABLE IF NOT EXISTS local_file (
  id               INTEGER PRIMARY KEY,
  path             TEXT NOT NULL UNIQUE,
  mtime            INTEGER NOT NULL,
  size             INTEGER NOT NULL,
  format           TEXT NOT NULL,
  duration_ms      INTEGER,
  artist           TEXT,
  title            TEXT,
  album            TEXT,
  track_number     INTEGER,
  metadata_source  TEXT,
  status           TEXT NOT NULL,
  spotify_track_id TEXT,
  match_confidence REAL,
  match_method     TEXT,
  first_seen_at    TEXT NOT NULL,
  last_scanned_at  TEXT,
  last_error       TEXT,
  last_run_id      INTEGER REFERENCES scan_run(id)
);
CREATE INDEX IF NOT EXISTS idx_local_file_status ON local_file(status);
CREATE INDEX IF NOT EXISTS idx_local_file_run    ON local_file(last_run_id);
CREATE INDEX IF NOT EXISTS idx_local_file_artist ON local_file(artist);

CREATE TABLE IF NOT EXISTS match_candidate (
  id                  INTEGER PRIMARY KEY,
  local_file_id       INTEGER NOT NULL REFERENCES local_file(id) ON DELETE CASCADE,
  spotify_track_id    TEXT NOT NULL,
  spotify_artist      TEXT NOT NULL,
  spotify_title       TEXT NOT NULL,
  spotify_album       TEXT,
  spotify_duration_ms INTEGER,
  artist_similarity   REAL NOT NULL,
  title_similarity    REAL NOT NULL,
  duration_delta_ms   INTEGER,
  confidence          REAL NOT NULL,
  rank                INTEGER NOT NULL,
  fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_match_candidate_file ON match_candidate(local_file_id, rank);

CREATE TABLE IF NOT EXISTS playlist (
  id                  INTEGER PRIMARY KEY,
  spotify_playlist_id TEXT NOT NULL UNIQUE,
  name                TEXT NOT NULL,
  chunk_index         INTEGER NOT NULL,
  alpha_range         TEXT,
  created_at          TEXT NOT NULL,
  track_count         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_track (
  playlist_id      INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
  local_file_id    INTEGER NOT NULL REFERENCES local_file(id) ON DELETE CASCADE,
  spotify_track_id TEXT NOT NULL,
  added_at         TEXT NOT NULL,
  PRIMARY KEY (playlist_id, local_file_id)
);

CREATE TABLE IF NOT EXISTS auth_token (
  key           TEXT PRIMARY KEY,
  access_token  TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  scope         TEXT NOT NULL,
  user_id       TEXT
);

CREATE TABLE IF NOT EXISTS setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@asynccontextmanager
async def connect(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    try:
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        yield conn
    finally:
        await conn.close()


async def init_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_db.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/db.py tests/test_db.py
git commit -m "feat: SQLite schema + connection"
```

---

### Task 5: Domain models (dataclasses)

**Files:**
- Create: `local2spoti/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Failing test**

`tests/test_models.py`:
```python
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
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_models.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/models.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FileStatus(str, Enum):
    NEW = "new"
    SCANNED = "scanned"
    MATCHED = "matched"
    REVIEW = "review"
    UNMATCHED = "unmatched"
    ERROR = "error"
    MISSING = "missing"


class MetadataSource(str, Enum):
    TAGS = "tags"
    FILENAME = "filename"
    ACOUSTID = "acoustid"
    MANUAL = "manual"
    NONE = "none"


@dataclass(slots=True)
class LocalFile:
    path: str
    mtime: int
    size: int
    format: str
    id: int | None = None
    duration_ms: int | None = None
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    track_number: int | None = None
    metadata_source: str | None = None
    status: FileStatus = FileStatus.NEW
    spotify_track_id: str | None = None
    match_confidence: float | None = None
    match_method: str | None = None
    first_seen_at: str | None = None
    last_scanned_at: str | None = None
    last_error: str | None = None
    last_run_id: int | None = None


@dataclass(order=True)
class MatchCandidate:
    # `order=True` plus negative confidence-as-sort-key handled below; we sort by -confidence
    sort_index: float = field(init=False, repr=False)
    spotify_track_id: str = ""
    spotify_artist: str = ""
    spotify_title: str = ""
    artist_similarity: float = 0.0
    title_similarity: float = 0.0
    confidence: float = 0.0
    rank: int = 0
    spotify_album: str | None = None
    spotify_duration_ms: int | None = None
    duration_delta_ms: int | None = None

    def __post_init__(self) -> None:
        self.sort_index = -self.confidence


@dataclass(slots=True)
class PlaylistChunk:
    id: int | None
    spotify_playlist_id: str | None
    name: str
    chunk_index: int
    alpha_range: str
    track_count: int = 0


@dataclass(slots=True)
class ScanProgress:
    stage: str
    processed: int
    total: int
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    errors: int = 0
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/models.py tests/test_models.py
git commit -m "feat: domain models"
```

---

### Task 6: Repository layer (async DB queries)

**Files:**
- Create: `local2spoti/repo.py`
- Create: `tests/test_repo.py`

- [ ] **Step 1: Failing test**

`tests/test_repo.py`:
```python
from datetime import UTC, datetime
import pytest

from local2spoti.db import connect, init_schema
from local2spoti.models import FileStatus, LocalFile
from local2spoti import repo


@pytest.fixture
async def conn(tmp_path):
    async with connect(tmp_path / "t.db") as c:
        await init_schema(c)
        yield c


async def test_upsert_new_file(conn):
    f = LocalFile(path="/a.mp3", mtime=10, size=100, format="mp3")
    fid = await repo.upsert_local_file(conn, f, now=datetime(2026, 5, 4, tzinfo=UTC))
    assert fid > 0
    got = await repo.get_local_file_by_path(conn, "/a.mp3")
    assert got.status == FileStatus.NEW
    assert got.first_seen_at is not None


async def test_upsert_unchanged_skips(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    f = LocalFile(path="/a.mp3", mtime=10, size=100, format="mp3")
    await repo.upsert_local_file(conn, f, now=now)
    changed = await repo.upsert_local_file(conn, f, now=now)
    assert changed is False  # no-op for unchanged file


async def test_change_detected_when_mtime_changes(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    f = LocalFile(path="/a.mp3", mtime=10, size=100, format="mp3")
    await repo.upsert_local_file(conn, f, now=now)
    f.mtime = 20
    changed = await repo.upsert_local_file(conn, f, now=now)
    assert changed is True
    got = await repo.get_local_file_by_path(conn, "/a.mp3")
    assert got.status == FileStatus.NEW


async def test_count_by_status(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    for i in range(3):
        await repo.upsert_local_file(
            conn,
            LocalFile(path=f"/{i}.mp3", mtime=i, size=1, format="mp3",
                      status=FileStatus.MATCHED if i < 2 else FileStatus.REVIEW),
            now=now,
        )
    counts = await repo.count_by_status(conn)
    assert counts[FileStatus.MATCHED] == 2
    assert counts[FileStatus.REVIEW] == 1


async def test_mark_missing(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    later = datetime(2026, 5, 5, tzinfo=UTC)
    await repo.upsert_local_file(conn,
        LocalFile(path="/seen.mp3", mtime=1, size=1, format="mp3",
                  status=FileStatus.MATCHED), now=now)
    await repo.upsert_local_file(conn,
        LocalFile(path="/gone.mp3", mtime=1, size=1, format="mp3",
                  status=FileStatus.MATCHED), now=now)
    # Touch only the seen file
    await repo.touch_last_scanned(conn, "/seen.mp3", later)
    n = await repo.mark_missing_files(conn, scan_started=later)
    assert n == 1
    gone = await repo.get_local_file_by_path(conn, "/gone.mp3")
    assert gone.status == FileStatus.MISSING
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_repo.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement repo**

`local2spoti/repo.py`:
```python
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

import aiosqlite

from .models import FileStatus, LocalFile, MatchCandidate

_INSERT_FILE = """
INSERT INTO local_file (
    path, mtime, size, format, duration_ms,
    artist, title, album, track_number, metadata_source,
    status, spotify_track_id, match_confidence, match_method,
    first_seen_at, last_scanned_at
) VALUES (
    :path, :mtime, :size, :format, :duration_ms,
    :artist, :title, :album, :track_number, :metadata_source,
    :status, :spotify_track_id, :match_confidence, :match_method,
    :first_seen_at, :last_scanned_at
)
"""

_SELECT_FILE_BY_PATH = """
SELECT id, path, mtime, size, format, duration_ms,
       artist, title, album, track_number, metadata_source,
       status, spotify_track_id, match_confidence, match_method,
       first_seen_at, last_scanned_at, last_error, last_run_id
FROM local_file WHERE path = ?
"""


def _row_to_local_file(row: tuple) -> LocalFile:
    return LocalFile(
        id=row[0], path=row[1], mtime=row[2], size=row[3], format=row[4],
        duration_ms=row[5], artist=row[6], title=row[7], album=row[8],
        track_number=row[9], metadata_source=row[10],
        status=FileStatus(row[11]), spotify_track_id=row[12],
        match_confidence=row[13], match_method=row[14],
        first_seen_at=row[15], last_scanned_at=row[16],
        last_error=row[17], last_run_id=row[18],
    )


async def get_local_file_by_path(conn: aiosqlite.Connection, path: str) -> LocalFile | None:
    cur = await conn.execute(_SELECT_FILE_BY_PATH, (path,))
    row = await cur.fetchone()
    return _row_to_local_file(row) if row else None


async def upsert_local_file(
    conn: aiosqlite.Connection,
    f: LocalFile,
    *,
    now: datetime,
) -> bool:
    """Insert or update a local file row.

    Returns True if a change was made (new file or content changed),
    False if (path, mtime, size) all matched existing row.
    """
    iso = now.isoformat()
    existing = await get_local_file_by_path(conn, f.path)
    if existing is None:
        await conn.execute(
            _INSERT_FILE,
            {
                "path": f.path, "mtime": f.mtime, "size": f.size, "format": f.format,
                "duration_ms": f.duration_ms,
                "artist": f.artist, "title": f.title, "album": f.album,
                "track_number": f.track_number, "metadata_source": f.metadata_source,
                "status": f.status.value, "spotify_track_id": f.spotify_track_id,
                "match_confidence": f.match_confidence, "match_method": f.match_method,
                "first_seen_at": iso, "last_scanned_at": iso,
            },
        )
        await conn.commit()
        return True
    if existing.mtime == f.mtime and existing.size == f.size:
        return False
    await conn.execute(
        """UPDATE local_file SET mtime=?, size=?, status='new', last_scanned_at=?
           WHERE path=?""",
        (f.mtime, f.size, iso, f.path),
    )
    await conn.commit()
    return True


async def touch_last_scanned(conn: aiosqlite.Connection, path: str, now: datetime) -> None:
    await conn.execute(
        "UPDATE local_file SET last_scanned_at=? WHERE path=?",
        (now.isoformat(), path),
    )
    await conn.commit()


async def mark_missing_files(conn: aiosqlite.Connection, *, scan_started: datetime) -> int:
    cur = await conn.execute(
        """UPDATE local_file SET status='missing'
           WHERE last_scanned_at < ? AND status != 'missing'""",
        (scan_started.isoformat(),),
    )
    await conn.commit()
    return cur.rowcount


async def count_by_status(conn: aiosqlite.Connection) -> dict[FileStatus, int]:
    cur = await conn.execute(
        "SELECT status, COUNT(*) FROM local_file GROUP BY status"
    )
    out: dict[FileStatus, int] = defaultdict(int)
    for status, n in await cur.fetchall():
        out[FileStatus(status)] = n
    return out


async def update_match(
    conn: aiosqlite.Connection,
    file_id: int,
    *,
    spotify_track_id: str,
    confidence: float,
    method: str,
    status: FileStatus = FileStatus.MATCHED,
) -> None:
    await conn.execute(
        """UPDATE local_file
           SET spotify_track_id=?, match_confidence=?, match_method=?, status=?
           WHERE id=?""",
        (spotify_track_id, confidence, method, status.value, file_id),
    )
    await conn.commit()


async def set_status(
    conn: aiosqlite.Connection, file_id: int, status: FileStatus,
    *, last_error: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE local_file SET status=?, last_error=? WHERE id=?",
        (status.value, last_error, file_id),
    )
    await conn.commit()


async def insert_candidates(
    conn: aiosqlite.Connection,
    file_id: int,
    candidates: Iterable[MatchCandidate],
    *,
    now: datetime,
) -> None:
    iso = now.isoformat()
    rows = [
        (
            file_id, c.spotify_track_id, c.spotify_artist, c.spotify_title,
            c.spotify_album, c.spotify_duration_ms,
            c.artist_similarity, c.title_similarity, c.duration_delta_ms,
            c.confidence, c.rank, iso,
        )
        for c in candidates
    ]
    await conn.executemany(
        """INSERT INTO match_candidate (
            local_file_id, spotify_track_id, spotify_artist, spotify_title,
            spotify_album, spotify_duration_ms,
            artist_similarity, title_similarity, duration_delta_ms,
            confidence, rank, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    await conn.commit()


async def list_files_by_status(
    conn: aiosqlite.Connection, status: FileStatus, *, limit: int = 100, offset: int = 0,
) -> list[LocalFile]:
    cur = await conn.execute(
        _SELECT_FILE_BY_PATH.replace("WHERE path = ?", "WHERE status = ? ORDER BY artist, album, track_number, title LIMIT ? OFFSET ?"),
        (status.value, limit, offset),
    )
    return [_row_to_local_file(r) for r in await cur.fetchall()]


async def list_unique_artists(conn: aiosqlite.Connection) -> list[str]:
    cur = await conn.execute(
        "SELECT DISTINCT artist FROM local_file WHERE status='scanned' AND artist IS NOT NULL"
    )
    return [r[0] for r in await cur.fetchall()]
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_repo.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/repo.py tests/test_repo.py
git commit -m "feat: repository layer"
```

---

## Phase 1 — Scanner

### Task 7: Filename fallback parser

**Files:**
- Create: `local2spoti/scanner.py` (parser portion)
- Create: `tests/test_scanner.py` (parser tests)

- [ ] **Step 1: Failing test**

`tests/test_scanner.py`:
```python
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
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_scanner.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement parser**

`local2spoti/scanner.py`:
```python
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTS = {".mp3", ".flac", ".aac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".wma"}

_PAT_TRACK_ARTIST_TITLE = re.compile(r"^\s*(\d{1,3})\s*[-_.]\s*(.+?)\s*-\s*(.+)$")
_PAT_ARTIST_TITLE = re.compile(r"^(.+?)\s*-\s*(.+)$")
_PAT_TRACK_TITLE = re.compile(r"^\s*(\d{1,3})[\s.\-_]+(.+)$")


def parse_filename(
    filename: str, *, parents: tuple[str, ...] = (),
) -> tuple[str | None, str | None, int | None]:
    """Parse `filename` (basename) into (artist, title, track_number).

    Tries patterns in order:
      1. "01 - Artist - Title.ext"
      2. "Artist - Title.ext"
      3. "01 - Title.ext"  (artist comes from parents[0])
      4. "01. Title.ext"   (artist comes from parents[0])
    """
    stem = Path(filename).stem

    m = _PAT_TRACK_ARTIST_TITLE.match(stem)
    if m:
        return m.group(2).strip(), m.group(3).strip(), int(m.group(1))

    m = _PAT_ARTIST_TITLE.match(stem)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None

    m = _PAT_TRACK_TITLE.match(stem)
    if m and parents:
        return parents[0], m.group(2).strip(), int(m.group(1))

    return None, None, None
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/scanner.py tests/test_scanner.py
git commit -m "feat: filename fallback parser"
```

---

### Task 8: Tag extraction with mutagen

**Files:**
- Modify: `local2spoti/scanner.py` (append `read_tags`)
- Modify: `tests/test_scanner.py` (append tests)
- Create: `tests/conftest.py` (audio fixture generator)

- [ ] **Step 1: Write conftest fixture**

`tests/conftest.py`:
```python
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
```

- [ ] **Step 2: Failing test**

Append to `tests/test_scanner.py`:
```python
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
```

- [ ] **Step 3: Run (fail)**

Run: `pytest tests/test_scanner.py::test_read_tags_mp3 -v`
Expected: FAIL.

- [ ] **Step 4: Implement `read_tags`**

Append to `local2spoti/scanner.py`:
```python
from dataclasses import dataclass
import mutagen


@dataclass(slots=True)
class ParsedMetadata:
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    track_number: int | None = None
    duration_ms: int | None = None


def _first(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _parse_track_no(value: object) -> int | None:
    s = _first(value)
    if not s:
        return None
    head = s.split("/")[0].strip()
    return int(head) if head.isdigit() else None


def read_tags(path: Path) -> ParsedMetadata:
    try:
        f = mutagen.File(str(path), easy=True)
    except Exception:
        return ParsedMetadata()
    if f is None:
        return ParsedMetadata()
    tags = dict(f.tags) if f.tags else {}
    duration_ms: int | None = None
    if f.info and getattr(f.info, "length", None):
        duration_ms = int(f.info.length * 1000)
    return ParsedMetadata(
        artist=_first(tags.get("artist")),
        title=_first(tags.get("title")),
        album=_first(tags.get("album")),
        track_number=_parse_track_no(tags.get("tracknumber")),
        duration_ms=duration_ms,
    )
```

- [ ] **Step 5: Run**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add local2spoti/scanner.py tests/test_scanner.py tests/conftest.py
git commit -m "feat: mutagen tag reading + audio fixtures"
```

---

### Task 9: Filesystem walker

**Files:**
- Modify: `local2spoti/scanner.py` (append `walk`)
- Modify: `tests/test_scanner.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_scanner.py`:
```python
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
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_scanner.py::test_walk_finds_audio_files -v`
Expected: FAIL.

- [ ] **Step 3: Implement walker**

Append to `local2spoti/scanner.py`:
```python
from typing import Iterator


def walk_audio_files(root: Path) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """Yield (file_path, parent_folders) tuples for all audio files under root.

    `parent_folders` is ordered from immediate parent outward, used by parse_filename.
    Uses os.scandir for speed at large library sizes.
    """
    def _walk(d: Path, parents: tuple[str, ...]) -> Iterator[tuple[Path, tuple[str, ...]]]:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(Path(entry.path), (entry.name, *parents))
            else:
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in AUDIO_EXTS:
                    yield Path(entry.path), parents

    yield from _walk(root, ())
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/scanner.py tests/test_scanner.py
git commit -m "feat: filesystem walker with os.scandir"
```

---

## Phase 2 — Spotify client

### Task 10: Token-bucket rate limiter

**Files:**
- Create: `local2spoti/ratelimit.py`
- Create: `tests/test_ratelimit.py`

- [ ] **Step 1: Failing test**

`tests/test_ratelimit.py`:
```python
import asyncio
import pytest

from local2spoti.ratelimit import TokenBucket


async def test_acquire_immediate_when_full():
    b = TokenBucket(rate=10, capacity=5)
    for _ in range(5):
        await b.acquire()  # all immediate


async def test_acquire_blocks_when_empty(monkeypatch):
    b = TokenBucket(rate=100, capacity=2)
    await b.acquire()
    await b.acquire()
    start = asyncio.get_event_loop().time()
    await b.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.005  # had to wait at least one refill


async def test_drain():
    b = TokenBucket(rate=10, capacity=5)
    b.drain()
    assert b.tokens == 0


async def test_set_pause_until_blocks():
    b = TokenBucket(rate=1000, capacity=5)
    loop = asyncio.get_event_loop()
    b.pause_for(0.05)
    start = loop.time()
    await b.acquire()
    assert loop.time() - start >= 0.04
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_ratelimit.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/ratelimit.py`:
```python
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Async token bucket. Capacity tokens; refills `rate` tokens per second."""

    def __init__(self, *, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self._last = time.monotonic()
        self._pause_until = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta > 0:
            self.tokens = min(self.capacity, self.tokens + delta * self.rate)
            self._last = now

    async def acquire(self, n: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._pause_until:
                    sleep = self._pause_until - now
                else:
                    self._refill()
                    if self.tokens >= n:
                        self.tokens -= n
                        return
                    sleep = (n - self.tokens) / self.rate
            await asyncio.sleep(max(sleep, 0.001))

    def drain(self) -> None:
        self.tokens = 0

    def pause_for(self, seconds: float) -> None:
        self._pause_until = max(self._pause_until, time.monotonic() + seconds)
        self.drain()
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_ratelimit.py -v`
Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/ratelimit.py tests/test_ratelimit.py
git commit -m "feat: token-bucket rate limiter"
```

---

### Task 11: Spotify HTTP client (read paths)

**Files:**
- Create: `local2spoti/spotify_client.py`
- Create: `tests/test_spotify_client.py`
- Create: `tests/fixtures/spotify/__init__.py` (empty)

- [ ] **Step 1: Failing test**

`tests/test_spotify_client.py`:
```python
import httpx
import pytest
import respx

from local2spoti.ratelimit import TokenBucket
from local2spoti.spotify_client import SpotifyClient


@pytest.fixture
def client():
    bucket = TokenBucket(rate=1000, capacity=100)
    return SpotifyClient(access_token="fake", bucket=bucket)


@respx.mock
async def test_search_tracks(client):
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [
            {"id": "abc", "name": "Around the World",
             "artists": [{"name": "Daft Punk"}],
             "album": {"name": "Homework"}, "duration_ms": 423000}
        ]}})
    )
    items = await client.search_tracks("Daft Punk", "Around the World", limit=5)
    assert len(items) == 1
    assert items[0]["id"] == "abc"


@respx.mock
async def test_search_artist(client):
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"artists": {"items": [
            {"id": "xyz", "name": "Daft Punk"}
        ]}})
    )
    artist = await client.search_artist("Daft Punk")
    assert artist["id"] == "xyz"


@respx.mock
async def test_artist_albums(client):
    respx.get("https://api.spotify.com/v1/artists/xyz/albums").mock(
        return_value=httpx.Response(200, json={"items": [
            {"id": "alb1", "name": "Homework"},
            {"id": "alb2", "name": "Discovery"},
        ], "next": None})
    )
    albums = await client.artist_albums("xyz")
    assert [a["id"] for a in albums] == ["alb1", "alb2"]


@respx.mock
async def test_albums_batch(client):
    respx.get("https://api.spotify.com/v1/albums").mock(
        return_value=httpx.Response(200, json={"albums": [
            {"id": "alb1", "name": "Homework",
             "tracks": {"items": [
                 {"id": "t1", "name": "Da Funk", "duration_ms": 322000,
                  "artists": [{"name": "Daft Punk"}]}
             ]}},
        ]})
    )
    albums = await client.albums_batch(["alb1"])
    assert albums[0]["tracks"]["items"][0]["id"] == "t1"


@respx.mock
async def test_429_respects_retry_after(client):
    route = respx.get("https://api.spotify.com/v1/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"tracks": {"items": []}}),
        ]
    )
    items = await client.search_tracks("a", "b")
    assert items == []
    assert route.call_count == 2
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_spotify_client.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement client**

`local2spoti/spotify_client.py`:
```python
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson

from .ratelimit import TokenBucket

_BASE = "https://api.spotify.com/v1"


class SpotifyError(Exception):
    pass


class SpotifyClient:
    def __init__(
        self,
        *,
        access_token: str,
        bucket: TokenBucket,
        timeout: float = 30.0,
    ) -> None:
        self._token = access_token
        self._bucket = bucket
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def set_access_token(self, token: str) -> None:
        self._token = token
        self._http.headers["Authorization"] = f"Bearer {token}"

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, *, json: Any | None = None) -> dict[str, Any]:
        return await self._request("POST", path, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            await self._bucket.acquire()
            content = orjson.dumps(json) if json is not None else None
            r = await self._http.request(
                method, path,
                params=params,
                content=content,
                headers={"Content-Type": "application/json"} if json is not None else None,
            )
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "1"))
                self._bucket.pause_for(wait)
                continue
            if r.status_code >= 500 and attempt < 4:
                await asyncio.sleep(min(2**attempt, 8) + 0.1 * attempt)
                attempt += 1
                continue
            if r.status_code >= 400:
                raise SpotifyError(f"{r.status_code} {method} {path}: {r.text[:200]}")
            return orjson.loads(r.content) if r.content else {}

    async def search_tracks(
        self, artist: str, title: str, *, limit: int = 5,
    ) -> list[dict[str, Any]]:
        q = f'track:"{title}" artist:"{artist}"'
        data = await self._get("/search", q=q, type="track", limit=limit)
        return data.get("tracks", {}).get("items", [])

    async def search_artist(self, name: str) -> dict[str, Any] | None:
        data = await self._get("/search", q=name, type="artist", limit=1)
        items = data.get("artists", {}).get("items", [])
        return items[0] if items else None

    async def artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = await self._get(
                f"/artists/{artist_id}/albums",
                include_groups="album,single,compilation",
                limit=50, offset=offset,
            )
            items = data.get("items", [])
            out.extend(items)
            if not data.get("next") or not items:
                return out
            offset += len(items)

    async def albums_batch(self, album_ids: list[str]) -> list[dict[str, Any]]:
        if not album_ids:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i : i + 20]
            data = await self._get("/albums", ids=",".join(chunk))
            out.extend(data.get("albums", []))
        return out

    async def create_playlist(self, user_id: str, name: str, *, public: bool = False) -> dict[str, Any]:
        return await self._post(
            f"/users/{user_id}/playlists",
            json={"name": name, "public": public},
        )

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            await self._post(
                f"/playlists/{playlist_id}/tracks",
                json={"uris": uris[i : i + 100]},
            )

    async def me(self) -> dict[str, Any]:
        return await self._get("/me")
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_spotify_client.py -v`
Expected: PASS (5).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/spotify_client.py tests/test_spotify_client.py tests/fixtures/spotify/__init__.py
git commit -m "feat: Spotify HTTP client with rate limit + 429 handling"
```

---

### Task 12: OAuth PKCE flow

**Files:**
- Create: `local2spoti/spotify_oauth.py`
- Create: `tests/test_spotify_oauth.py`

- [ ] **Step 1: Failing test**

`tests/test_spotify_oauth.py`:
```python
import httpx
import respx
import pytest

from local2spoti.spotify_oauth import build_authorize_url, exchange_code, refresh_token, PKCE


def test_build_authorize_url():
    pkce = PKCE.generate()
    url = build_authorize_url(
        client_id="cid", redirect_uri="http://127.0.0.1:8000/callback",
        scope="x y", state="st", pkce=pkce,
    )
    assert "client_id=cid" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "scope=x+y" in url


@respx.mock
async def test_exchange_code():
    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "atk", "refresh_token": "rtk",
            "expires_in": 3600, "scope": "x y", "token_type": "Bearer",
        })
    )
    pkce = PKCE.generate()
    out = await exchange_code(
        code="code123", client_id="cid",
        redirect_uri="http://127.0.0.1:8000/callback", pkce=pkce,
    )
    assert out["access_token"] == "atk"
    assert out["refresh_token"] == "rtk"


@respx.mock
async def test_refresh_token():
    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new", "expires_in": 3600,
            "scope": "x", "token_type": "Bearer",
        })
    )
    out = await refresh_token(refresh="rtk", client_id="cid")
    assert out["access_token"] == "new"


def test_pkce_verifier_length():
    p = PKCE.generate()
    assert 43 <= len(p.verifier) <= 128
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_spotify_oauth.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/spotify_oauth.py`:
```python
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

DEFAULT_SCOPE = (
    "playlist-modify-private playlist-modify-public "
    "playlist-read-private user-read-private"
)


@dataclass(frozen=True)
class PKCE:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> "PKCE":
        verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return cls(verifier=verifier, challenge=challenge)


def build_authorize_url(
    *, client_id: str, redirect_uri: str, scope: str, state: str, pkce: PKCE,
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(
    *, code: str, client_id: str, redirect_uri: str, pkce: PKCE,
) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as h:
        r = await h.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": pkce.verifier,
            },
        )
        r.raise_for_status()
        return r.json()


async def refresh_token(*, refresh: str, client_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as h:
        r = await h.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_spotify_oauth.py -v`
Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/spotify_oauth.py tests/test_spotify_oauth.py
git commit -m "feat: Spotify PKCE OAuth flow"
```

---

## Phase 3 — Matching

### Task 13: Confidence scoring

**Files:**
- Create: `local2spoti/matcher.py`
- Create: `tests/test_matcher.py`

- [ ] **Step 1: Failing test**

`tests/test_matcher.py`:
```python
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
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_matcher.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/matcher.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .normalize import similarity


class Threshold(str, Enum):
    STRICT = "strict"
    BALANCED = "balanced"
    LOOSE = "loose"


Decision = Literal["auto", "review", "unmatched"]


@dataclass(slots=True)
class Score:
    artist_similarity: float
    title_similarity: float
    album_match: bool
    duration_delta_ms: int | None
    confidence: float


def score_candidate(
    *,
    local_artist: str,
    local_title: str,
    local_album: str | None,
    local_duration_ms: int | None,
    spotify_artist: str,
    spotify_title: str,
    spotify_album: str | None,
    spotify_duration_ms: int | None,
) -> Score:
    a_sim = similarity(local_artist, spotify_artist)
    t_sim = similarity(local_title, spotify_title)

    album_match = False
    album_bonus = 0.0
    if local_album and spotify_album:
        if similarity(local_album, spotify_album) >= 0.90:
            album_match = True
            album_bonus = 0.05

    delta: int | None = None
    dur_bonus = 0.0
    if local_duration_ms and spotify_duration_ms:
        delta = abs(local_duration_ms - spotify_duration_ms)
        if delta <= 3000:
            dur_bonus = 0.10
        elif delta <= 7000:
            dur_bonus = 0.05

    confidence = 0.45 * a_sim + 0.45 * t_sim + album_bonus + dur_bonus
    confidence = min(1.0, confidence)
    return Score(
        artist_similarity=a_sim,
        title_similarity=t_sim,
        album_match=album_match,
        duration_delta_ms=delta,
        confidence=confidence,
    )


def decide(
    *,
    artist_sim: float,
    title_sim: float,
    album_match: bool,
    duration_delta_ms: int | None,
    threshold: Threshold,
) -> Decision:
    dur_within = duration_delta_ms is not None and duration_delta_ms <= 3000
    dur_within_5s = duration_delta_ms is not None and duration_delta_ms <= 5000

    if threshold is Threshold.STRICT:
        if artist_sim >= 0.95 and title_sim >= 0.95 and dur_within:
            return "auto"
    elif threshold is Threshold.BALANCED:
        if artist_sim >= 0.90 and title_sim >= 0.90 and (album_match or dur_within_5s):
            return "auto"
    else:  # LOOSE
        if artist_sim >= 0.80 and title_sim >= 0.80:
            return "auto"

    score = 0.45 * artist_sim + 0.45 * title_sim
    return "review" if score >= 0.50 else "unmatched"
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_matcher.py -v`
Expected: PASS (7).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/matcher.py tests/test_matcher.py
git commit -m "feat: confidence scoring + threshold decisions"
```

---

### Task 14: Artist-first matching

**Files:**
- Create: `local2spoti/artist_match.py`
- Create: `tests/test_artist_match.py`

- [ ] **Step 1: Failing test**

`tests/test_artist_match.py`:
```python
from unittest.mock import AsyncMock

import pytest

from local2spoti.artist_match import match_artist_group
from local2spoti.matcher import Threshold
from local2spoti.models import LocalFile, FileStatus


def _file(artist: str, title: str, dur: int = 423000) -> LocalFile:
    return LocalFile(
        path=f"/{title}.mp3", mtime=1, size=1, format="mp3",
        artist=artist, title=title, duration_ms=dur, status=FileStatus.SCANNED,
    )


@pytest.fixture
def fake_client():
    client = AsyncMock()
    client.search_artist.return_value = {"id": "artist1", "name": "Daft Punk"}
    client.artist_albums.return_value = [
        {"id": "alb1", "name": "Homework"},
    ]
    client.albums_batch.return_value = [
        {"id": "alb1", "name": "Homework", "tracks": {"items": [
            {"id": "t1", "name": "Around the World", "duration_ms": 423000,
             "artists": [{"name": "Daft Punk"}]},
            {"id": "t2", "name": "Da Funk", "duration_ms": 322000,
             "artists": [{"name": "Daft Punk"}]},
        ]}},
    ]
    return client


async def test_artist_match_finds_correct_track(fake_client):
    files = [_file("Daft Punk", "Around the World", 423000)]
    results = await match_artist_group(
        client=fake_client, artist="Daft Punk", files=files, threshold=Threshold.BALANCED,
    )
    [r] = results
    assert r.decision == "auto"
    assert r.top_candidate.spotify_track_id == "t1"


async def test_artist_match_review_for_typo(fake_client):
    files = [_file("Daft Punk", "Arond Da World", 423000)]
    results = await match_artist_group(
        client=fake_client, artist="Daft Punk", files=files, threshold=Threshold.BALANCED,
    )
    [r] = results
    assert r.decision in ("review", "auto")
    assert r.top_candidate is not None


async def test_artist_match_unmatched_when_no_match(fake_client):
    files = [_file("Daft Punk", "Some Track Not In Catalog", 200000)]
    results = await match_artist_group(
        client=fake_client, artist="Daft Punk", files=files, threshold=Threshold.STRICT,
    )
    [r] = results
    assert r.decision in ("review", "unmatched")


async def test_artist_match_no_artist_results():
    client = AsyncMock()
    client.search_artist.return_value = None
    files = [_file("Mystery Artist", "Track")]
    results = await match_artist_group(
        client=client, artist="Mystery Artist", files=files, threshold=Threshold.BALANCED,
    )
    [r] = results
    assert r.decision == "no_artist"  # caller must run per-track fallback
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_artist_match.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/artist_match.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .matcher import Threshold, decide, score_candidate
from .models import LocalFile, MatchCandidate
from .spotify_client import SpotifyClient

ArtistDecision = Literal["auto", "review", "unmatched", "no_artist"]


@dataclass(slots=True)
class FileMatchResult:
    file: LocalFile
    decision: ArtistDecision
    top_candidate: MatchCandidate | None
    candidates: list[MatchCandidate]


def _track_to_candidate(
    track: dict, *, file: LocalFile, rank: int = 0,
) -> MatchCandidate | None:
    if not file.artist or not file.title:
        return None
    artists = track.get("artists", [{}])
    spotify_artist = artists[0].get("name", "") if artists else ""
    spotify_title = track.get("name", "")
    spotify_album = (track.get("album") or {}).get("name") if track.get("album") else None
    spotify_dur = track.get("duration_ms")
    s = score_candidate(
        local_artist=file.artist,
        local_title=file.title,
        local_album=file.album,
        local_duration_ms=file.duration_ms,
        spotify_artist=spotify_artist,
        spotify_title=spotify_title,
        spotify_album=spotify_album,
        spotify_duration_ms=spotify_dur,
    )
    return MatchCandidate(
        spotify_track_id=track["id"],
        spotify_artist=spotify_artist,
        spotify_title=spotify_title,
        spotify_album=spotify_album,
        spotify_duration_ms=spotify_dur,
        artist_similarity=s.artist_similarity,
        title_similarity=s.title_similarity,
        duration_delta_ms=s.duration_delta_ms,
        confidence=s.confidence,
        rank=rank,
    )


async def match_artist_group(
    *,
    client: SpotifyClient,
    artist: str,
    files: list[LocalFile],
    threshold: Threshold,
) -> list[FileMatchResult]:
    """Match every file in `files` against the Spotify catalog of `artist`."""
    spotify_artist = await client.search_artist(artist)
    if spotify_artist is None:
        return [FileMatchResult(f, "no_artist", None, []) for f in files]

    albums = await client.artist_albums(spotify_artist["id"])
    album_ids = [a["id"] for a in albums]
    full_albums = await client.albums_batch(album_ids)

    catalog: list[dict] = []
    seen_ids: set[str] = set()
    for alb in full_albums:
        for t in alb.get("tracks", {}).get("items", []):
            if t["id"] in seen_ids:
                continue
            seen_ids.add(t["id"])
            t = {**t, "album": {"name": alb.get("name")}}
            catalog.append(t)

    results: list[FileMatchResult] = []
    for f in files:
        scored = [c for c in (_track_to_candidate(t, file=f) for t in catalog) if c]
        scored.sort(key=lambda c: -c.confidence)
        top5 = scored[:5]
        for i, c in enumerate(top5, start=1):
            c.rank = i
        if not top5:
            results.append(FileMatchResult(f, "unmatched", None, []))
            continue
        top = top5[0]
        decision = decide(
            artist_sim=top.artist_similarity,
            title_sim=top.title_similarity,
            album_match=(top.spotify_album is not None and f.album is not None),
            duration_delta_ms=top.duration_delta_ms,
            threshold=threshold,
        )
        results.append(FileMatchResult(f, decision, top, top5))
    return results
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_artist_match.py -v`
Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/artist_match.py tests/test_artist_match.py
git commit -m "feat: artist-first matching against catalog"
```

---

### Task 15: Per-track fallback search

**Files:**
- Modify: `local2spoti/artist_match.py` (append `match_per_track`)
- Modify: `tests/test_artist_match.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_artist_match.py`:
```python
from local2spoti.artist_match import match_per_track


async def test_per_track_fallback_finds_match():
    client = AsyncMock()
    client.search_tracks.return_value = [
        {"id": "t1", "name": "Around the World", "duration_ms": 423000,
         "artists": [{"name": "Daft Punk"}],
         "album": {"name": "Homework"}},
    ]
    f = LocalFile(path="/x.mp3", mtime=1, size=1, format="mp3",
                  artist="Daft Punk", title="Around the World",
                  duration_ms=423000, status=FileStatus.SCANNED)
    [r] = await match_per_track(client=client, files=[f], threshold=Threshold.BALANCED)
    assert r.decision == "auto"
    assert r.top_candidate.spotify_track_id == "t1"


async def test_per_track_fallback_no_results():
    client = AsyncMock()
    client.search_tracks.return_value = []
    f = LocalFile(path="/x.mp3", mtime=1, size=1, format="mp3",
                  artist="Mystery", title="Mystery", status=FileStatus.SCANNED)
    [r] = await match_per_track(client=client, files=[f], threshold=Threshold.BALANCED)
    assert r.decision == "unmatched"
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_artist_match.py::test_per_track_fallback_finds_match -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `local2spoti/artist_match.py`:
```python
async def match_per_track(
    *,
    client: SpotifyClient,
    files: list[LocalFile],
    threshold: Threshold,
) -> list[FileMatchResult]:
    out: list[FileMatchResult] = []
    for f in files:
        if not f.artist or not f.title:
            out.append(FileMatchResult(f, "unmatched", None, []))
            continue
        items = await client.search_tracks(f.artist, f.title, limit=5)
        scored = [c for c in (_track_to_candidate(t, file=f) for t in items) if c]
        scored.sort(key=lambda c: -c.confidence)
        top5 = scored[:5]
        for i, c in enumerate(top5, start=1):
            c.rank = i
        if not top5:
            out.append(FileMatchResult(f, "unmatched", None, []))
            continue
        top = top5[0]
        decision = decide(
            artist_sim=top.artist_similarity,
            title_sim=top.title_similarity,
            album_match=(top.spotify_album is not None and f.album is not None),
            duration_delta_ms=top.duration_delta_ms,
            threshold=threshold,
        )
        out.append(FileMatchResult(f, decision, top, top5))
    return out
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_artist_match.py -v`
Expected: PASS (6 total).

- [ ] **Step 5: Commit**

```bash
git add local2spoti/artist_match.py tests/test_artist_match.py
git commit -m "feat: per-track fallback for artists with no Spotify hit"
```

---

## Phase 4 — Pipeline orchestration

### Task 16: Event coalescer (WebSocket broadcaster)

**Files:**
- Create: `local2spoti/events.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Failing test**

`tests/test_events.py`:
```python
import asyncio
import pytest

from local2spoti.events import EventBus, ProgressEvent


async def test_subscribers_receive_events():
    bus = EventBus(min_interval=0.0)
    queue = await bus.subscribe()
    await bus.publish(ProgressEvent(stage="discovery", processed=1, total=10))
    e = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert e.processed == 1


async def test_coalescing_drops_intermediate():
    bus = EventBus(min_interval=0.05)
    queue = await bus.subscribe()
    for i in range(10):
        await bus.publish(ProgressEvent(stage="match", processed=i, total=10))
    await asyncio.sleep(0.1)
    await bus.flush()
    received: list[int] = []
    while not queue.empty():
        received.append(queue.get_nowait().processed)
    assert len(received) <= 3
    assert received[-1] == 9  # the last update is always preserved


async def test_unsubscribe():
    bus = EventBus(min_interval=0.0)
    q = await bus.subscribe()
    await bus.unsubscribe(q)
    await bus.publish(ProgressEvent(stage="x", processed=1, total=1))
    assert q.empty()
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_events.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/events.py`:
```python
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class ProgressEvent:
    stage: str
    processed: int
    total: int
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    errors: int = 0
    message: str | None = None


class EventBus:
    """Pub/sub with per-stage coalescing.

    Calls to `publish` with the same `stage` within `min_interval` seconds
    are coalesced — the latest event wins. The final event for a stage is
    always flushed via `flush()` or natural timing.
    """

    def __init__(self, *, min_interval: float = 0.1) -> None:
        self._subscribers: set[asyncio.Queue[ProgressEvent]] = set()
        self._pending: dict[str, ProgressEvent] = {}
        self._last_emit: dict[str, float] = {}
        self._min_interval = min_interval
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[ProgressEvent]:
        q: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[ProgressEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: ProgressEvent) -> None:
        now = time.monotonic()
        last = self._last_emit.get(event.stage, 0.0)
        if now - last >= self._min_interval:
            self._last_emit[event.stage] = now
            await self._fan_out(event)
            self._pending.pop(event.stage, None)
        else:
            self._pending[event.stage] = event

    async def flush(self) -> None:
        """Emit any pending coalesced events. Call at end of stage and on shutdown."""
        for stage, event in list(self._pending.items()):
            self._last_emit[stage] = time.monotonic()
            await self._fan_out(event)
        self._pending.clear()

    async def _fan_out(self, event: ProgressEvent) -> None:
        async with self._lock:
            queues = list(self._subscribers)
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/events.py tests/test_events.py
git commit -m "feat: event bus with per-stage coalescing"
```

---

### Task 17: Pipeline orchestration

**Files:**
- Create: `local2spoti/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Failing test**

`tests/test_pipeline.py`:
```python
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from local2spoti.db import connect, init_schema
from local2spoti.events import EventBus
from local2spoti.matcher import Threshold
from local2spoti.models import FileStatus
from local2spoti.pipeline import run_scan
from local2spoti import repo


@pytest.fixture
def fake_client():
    c = AsyncMock()
    c.search_artist.return_value = {"id": "art1", "name": "Daft Punk"}
    c.artist_albums.return_value = [{"id": "alb1", "name": "Homework"}]
    c.albums_batch.return_value = [{
        "id": "alb1", "name": "Homework", "tracks": {"items": [
            {"id": "t1", "name": "Around the World", "duration_ms": 423000,
             "artists": [{"name": "Daft Punk"}]},
        ]},
    }]
    return c


async def test_scan_e2e(tmp_path, fake_client, make_mp3):
    library = tmp_path / "lib"
    library.mkdir()
    (library / "Daft Punk").mkdir()
    make_mp3 = make_mp3  # alias
    # re-implemented inline because fixture root is different
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "1", "-q:a", "9",
        "-metadata", "artist=Daft Punk",
        "-metadata", "title=Around the World",
        "-metadata", "album=Homework",
        str(library / "Daft Punk" / "01 - Around the World.mp3"),
    ], check=True, capture_output=True)

    db_path = tmp_path / "state.db"
    bus = EventBus(min_interval=0.0)
    async with connect(db_path) as conn:
        await init_schema(conn)
        result = await run_scan(
            conn=conn,
            client=fake_client,
            library_root=library,
            threshold=Threshold.BALANCED,
            bus=bus,
        )
        assert result.matched >= 1
        counts = await repo.count_by_status(conn)
        assert counts.get(FileStatus.MATCHED, 0) >= 1


async def test_scan_resumability(tmp_path, fake_client, make_mp3):
    """Re-running a scan after completion processes 0 files."""
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    library = tmp_path / "lib"
    (library / "Daft Punk").mkdir(parents=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "1", "-q:a", "9",
        "-metadata", "artist=Daft Punk",
        "-metadata", "title=Around the World",
        str(library / "Daft Punk" / "01 - Around the World.mp3"),
    ], check=True, capture_output=True)

    db_path = tmp_path / "state.db"
    async with connect(db_path) as conn:
        await init_schema(conn)
        await run_scan(conn=conn, client=fake_client, library_root=library,
                       threshold=Threshold.BALANCED, bus=EventBus(min_interval=0.0))
        result2 = await run_scan(conn=conn, client=fake_client, library_root=library,
                                  threshold=Threshold.BALANCED, bus=EventBus(min_interval=0.0))
    assert result2.processed_files == 0
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement pipeline**

`local2spoti/pipeline.py`:
```python
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from . import repo
from .artist_match import FileMatchResult, match_artist_group, match_per_track
from .events import EventBus, ProgressEvent
from .matcher import Threshold
from .models import FileStatus, LocalFile, MetadataSource
from .normalize import normalize_artist
from .scanner import AUDIO_EXTS, parse_filename, read_tags, walk_audio_files
from .spotify_client import SpotifyClient


@dataclass(slots=True)
class ScanResult:
    processed_files: int
    matched: int
    review: int
    unmatched: int
    errors: int


async def _stage_discovery(
    conn: aiosqlite.Connection, library_root: Path, *, now: datetime,
    bus: EventBus,
) -> int:
    """Walk filesystem, upsert local_file rows. Returns count of new/changed files."""
    changed = 0
    seen = 0
    for path, parents in walk_audio_files(library_root):
        seen += 1
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        f = LocalFile(
            path=str(path), mtime=int(st.st_mtime), size=st.st_size,
            format=path.suffix.lower().lstrip("."),
        )
        if await repo.upsert_local_file(conn, f, now=now):
            changed += 1
        await repo.touch_last_scanned(conn, str(path), now)
        if seen % 200 == 0:
            await bus.publish(ProgressEvent(stage="discovery", processed=seen, total=seen))
    await bus.publish(ProgressEvent(stage="discovery", processed=seen, total=seen))
    await repo.mark_missing_files(conn, scan_started=now)
    return changed


async def _stage_metadata(
    conn: aiosqlite.Connection, *, bus: EventBus,
) -> None:
    """For every status='new' file, read tags + filename fallback. Set status='scanned'."""
    cur = await conn.execute(
        "SELECT id, path FROM local_file WHERE status='new'"
    )
    rows = await cur.fetchall()
    total = len(rows)
    if total == 0:
        return
    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(16)
    processed = 0

    async def process(file_id: int, path_str: str) -> None:
        nonlocal processed
        async with sem:
            path = Path(path_str)
            md = await loop.run_in_executor(None, read_tags, path)
            if not md.artist or not md.title:
                a, t, n = parse_filename(
                    path.name,
                    parents=tuple(p.name for p in path.parents[:-1]),
                )
                if a and t:
                    md.artist = md.artist or a
                    md.title = md.title or t
                    md.track_number = md.track_number or n
                    source = MetadataSource.FILENAME.value
                else:
                    await conn.execute(
                        "UPDATE local_file SET status='unmatched', metadata_source='none' WHERE id=?",
                        (file_id,),
                    )
                    await conn.commit()
                    processed += 1
                    return
            else:
                source = MetadataSource.TAGS.value
            await conn.execute(
                """UPDATE local_file SET artist=?, title=?, album=?, track_number=?,
                   duration_ms=?, metadata_source=?, status='scanned' WHERE id=?""",
                (md.artist, md.title, md.album, md.track_number, md.duration_ms,
                 source, file_id),
            )
            await conn.commit()
            processed += 1
            if processed % 50 == 0:
                await bus.publish(ProgressEvent(stage="metadata", processed=processed, total=total))

    await asyncio.gather(*[process(r[0], r[1]) for r in rows])
    await bus.publish(ProgressEvent(stage="metadata", processed=total, total=total))


async def _stage_match(
    conn: aiosqlite.Connection,
    client: SpotifyClient,
    threshold: Threshold,
    *,
    bus: EventBus,
    now: datetime,
) -> dict[str, int]:
    """Group scanned files by artist; run artist-first match; per-track fallback."""
    cur = await conn.execute(
        "SELECT id, path, artist, title, album, duration_ms FROM local_file WHERE status='scanned'"
    )
    rows = await cur.fetchall()
    if not rows:
        return {"matched": 0, "review": 0, "unmatched": 0}
    groups: dict[str, list[LocalFile]] = defaultdict(list)
    for r in rows:
        f = LocalFile(
            id=r[0], path=r[1], mtime=0, size=0, format="",
            artist=r[2], title=r[3], album=r[4], duration_ms=r[5],
            status=FileStatus.SCANNED,
        )
        groups[normalize_artist(r[2] or "")].append(f)

    counts = {"matched": 0, "review": 0, "unmatched": 0}
    total = len(rows)
    processed = 0

    sem = asyncio.Semaphore(12)

    async def process_artist(_: str, files: list[LocalFile]) -> None:
        nonlocal processed
        async with sem:
            results = await match_artist_group(
                client=client, artist=files[0].artist or "", files=files,
                threshold=threshold,
            )
            no_artist_files = [r.file for r in results if r.decision == "no_artist"]
            if no_artist_files:
                fallbacks = await match_per_track(
                    client=client, files=no_artist_files, threshold=threshold,
                )
                results = [r for r in results if r.decision != "no_artist"] + fallbacks
            await _persist_matches(conn, results, now=now, counts=counts)
            processed += len(files)
            await bus.publish(ProgressEvent(
                stage="match", processed=processed, total=total,
                matched=counts["matched"], review=counts["review"], unmatched=counts["unmatched"],
            ))

    await asyncio.gather(*[process_artist(a, fs) for a, fs in groups.items()])
    await bus.publish(ProgressEvent(
        stage="match", processed=total, total=total,
        matched=counts["matched"], review=counts["review"], unmatched=counts["unmatched"],
    ))
    return counts


async def _persist_matches(
    conn: aiosqlite.Connection, results: list[FileMatchResult], *,
    now: datetime, counts: dict[str, int],
) -> None:
    for r in results:
        assert r.file.id is not None
        if r.decision == "auto" and r.top_candidate:
            await repo.update_match(
                conn, r.file.id,
                spotify_track_id=r.top_candidate.spotify_track_id,
                confidence=r.top_candidate.confidence,
                method="auto",
            )
            counts["matched"] += 1
        elif r.decision == "review" and r.candidates:
            await repo.set_status(conn, r.file.id, FileStatus.REVIEW)
            await repo.insert_candidates(conn, r.file.id, r.candidates, now=now)
            counts["review"] += 1
        else:
            await repo.set_status(conn, r.file.id, FileStatus.UNMATCHED)
            counts["unmatched"] += 1


async def run_scan(
    *,
    conn: aiosqlite.Connection,
    client: SpotifyClient,
    library_root: Path,
    threshold: Threshold,
    bus: EventBus,
) -> ScanResult:
    now = datetime.now(UTC)
    cur = await conn.execute(
        """INSERT INTO scan_run (root_path, started_at, status, threshold)
           VALUES (?, ?, 'running', ?)""",
        (str(library_root), now.isoformat(), threshold.value),
    )
    run_id = cur.lastrowid
    await conn.commit()

    try:
        changed = await _stage_discovery(conn, library_root, now=now, bus=bus)
        await _stage_metadata(conn, bus=bus)
        counts = await _stage_match(conn, client, threshold, bus=bus, now=now)

        await conn.execute(
            """UPDATE scan_run SET finished_at=?, status='completed',
               total_files=?, matched_count=?, review_count=?, unmatched_count=?
               WHERE id=?""",
            (datetime.now(UTC).isoformat(), changed,
             counts["matched"], counts["review"], counts["unmatched"], run_id),
        )
        await conn.commit()
        await bus.flush()
        return ScanResult(
            processed_files=changed,
            matched=counts["matched"],
            review=counts["review"],
            unmatched=counts["unmatched"],
            errors=0,
        )
    except Exception as e:
        await conn.execute(
            "UPDATE scan_run SET status='failed', error_message=? WHERE id=?",
            (str(e)[:500], run_id),
        )
        await conn.commit()
        raise
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/pipeline.py tests/test_pipeline.py
git commit -m "feat: pipeline orchestration with discovery/metadata/match stages"
```

---

## Phase 5 — Playlist upload

### Task 18: Playlist chunking

**Files:**
- Create: `local2spoti/playlist.py`
- Create: `tests/test_playlist.py`

- [ ] **Step 1: Failing test**

`tests/test_playlist.py`:
```python
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
    # Each chunk has a coherent alpha range
    for c in chunks:
        assert c.alpha_range  # non-empty


def test_chunk_index_starts_at_one():
    chunks = chunk_files_alpha(_files("A", "B"), chunk_size=9000)
    assert chunks[0].chunk_index == 1


def test_chunk_name_contains_index_and_total():
    files = _files(*[f"A{i:04d}" for i in range(10000)] + [f"Z{i:04d}" for i in range(2000)])
    chunks = chunk_files_alpha(files, chunk_size=9000)
    names = [c.name for c in chunks]
    for i, n in enumerate(names, start=1):
        assert f"{i}/{len(chunks)}" in n
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_playlist.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/playlist.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

from .normalize import alpha_bucket


@dataclass(slots=True)
class PlaylistChunkPlan:
    chunk_index: int
    alpha_range: str
    name: str
    track_ids: list[str]


def chunk_files_alpha(
    files: list[dict],
    *,
    chunk_size: int = 9000,
) -> list[PlaylistChunkPlan]:
    """Sort files by artist and split into alpha-keyed chunks of `chunk_size`.

    Each input dict has at least 'artist' and 'spotify_track_id' keys.
    """
    sorted_files = sorted(files, key=lambda f: (f.get("artist") or "").lower())
    chunks: list[PlaylistChunkPlan] = []
    buffer: list[dict] = []
    for f in sorted_files:
        buffer.append(f)
        if len(buffer) >= chunk_size:
            chunks.append(_buffer_to_chunk(buffer, chunk_index=len(chunks) + 1))
            buffer = []
    if buffer:
        chunks.append(_buffer_to_chunk(buffer, chunk_index=len(chunks) + 1))

    total = len(chunks)
    for c in chunks:
        c.name = f"Local Library {c.chunk_index}/{total} ({c.alpha_range})"
    return chunks


def _buffer_to_chunk(buffer: list[dict], *, chunk_index: int) -> PlaylistChunkPlan:
    first = alpha_bucket(buffer[0]["artist"] or "")
    last = alpha_bucket(buffer[-1]["artist"] or "")
    alpha_range = first if first == last else f"{first}-{last}"
    return PlaylistChunkPlan(
        chunk_index=chunk_index,
        alpha_range=alpha_range,
        name=f"Local Library {chunk_index} ({alpha_range})",
        track_ids=[f["spotify_track_id"] for f in buffer],
    )
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_playlist.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/playlist.py tests/test_playlist.py
git commit -m "feat: alpha-keyed playlist chunking"
```

---

### Task 19: Playlist upload (DB + Spotify wiring)

**Files:**
- Modify: `local2spoti/playlist.py`
- Modify: `tests/test_playlist.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_playlist.py`:
```python
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from local2spoti.db import connect, init_schema
from local2spoti.playlist import push_matched_to_spotify
from local2spoti.models import FileStatus
from local2spoti import repo
from local2spoti.models import LocalFile


async def test_push_creates_playlists_and_inserts_track_rows(tmp_path):
    db = tmp_path / "t.db"
    client = AsyncMock()
    client.me.return_value = {"id": "user1"}
    client.create_playlist.return_value = {"id": "spotPlay1", "name": "Local Library 1/1"}
    client.add_tracks.return_value = None

    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        for i in range(3):
            await repo.upsert_local_file(conn, LocalFile(
                path=f"/{i}.mp3", mtime=1, size=1, format="mp3",
                artist="Daft Punk", title=f"T{i}",
                spotify_track_id=f"track{i}",
                status=FileStatus.MATCHED,
            ), now=now)
        result = await push_matched_to_spotify(conn=conn, client=client)
        assert result.added == 3
        assert client.add_tracks.await_count == 1
        cur = await conn.execute("SELECT COUNT(*) FROM playlist_track")
        assert (await cur.fetchone())[0] == 3
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_playlist.py::test_push_creates_playlists_and_inserts_track_rows -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `local2spoti/playlist.py`:
```python
from datetime import UTC, datetime

import aiosqlite

from .spotify_client import SpotifyClient


@dataclass(slots=True)
class PushResult:
    playlists_created: int
    added: int


async def push_matched_to_spotify(
    *, conn: aiosqlite.Connection, client: SpotifyClient,
) -> PushResult:
    """For all matched files not yet in any playlist, create chunked playlists and add."""
    cur = await conn.execute(
        """SELECT lf.id, lf.artist, lf.album, lf.track_number, lf.title, lf.spotify_track_id
           FROM local_file lf
           LEFT JOIN playlist_track pt ON pt.local_file_id = lf.id
           WHERE lf.status='matched' AND pt.local_file_id IS NULL
           ORDER BY lf.artist, lf.album, lf.track_number, lf.title"""
    )
    rows = await cur.fetchall()
    if not rows:
        return PushResult(playlists_created=0, added=0)

    files_dicts = [
        {"file_id": r[0], "artist": r[1], "spotify_track_id": r[5]}
        for r in rows
    ]
    chunks = chunk_files_alpha(files_dicts, chunk_size=9000)

    me = await client.me()
    user_id = me["id"]
    now_iso = datetime.now(UTC).isoformat()
    created = 0
    added = 0

    for chunk in chunks:
        sp = await client.create_playlist(user_id, chunk.name, public=False)
        spotify_playlist_id = sp["id"]
        cur = await conn.execute(
            """INSERT INTO playlist (spotify_playlist_id, name, chunk_index, alpha_range,
                                     created_at, track_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (spotify_playlist_id, chunk.name, chunk.chunk_index, chunk.alpha_range,
             now_iso, len(chunk.track_ids)),
        )
        playlist_db_id = cur.lastrowid
        await conn.commit()

        uris = [f"spotify:track:{tid}" for tid in chunk.track_ids]
        await client.add_tracks(spotify_playlist_id, uris)

        # Re-query the file_ids in chunk order
        chunk_files = [f for f in files_dicts if f["spotify_track_id"] in set(chunk.track_ids)]
        rows_to_insert = [
            (playlist_db_id, f["file_id"], f["spotify_track_id"], now_iso)
            for f in chunk_files
        ]
        await conn.executemany(
            """INSERT INTO playlist_track (playlist_id, local_file_id, spotify_track_id, added_at)
               VALUES (?, ?, ?, ?)""",
            rows_to_insert,
        )
        await conn.commit()
        created += 1
        added += len(chunk.track_ids)

    return PushResult(playlists_created=created, added=added)
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_playlist.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/playlist.py tests/test_playlist.py
git commit -m "feat: push matched tracks to Spotify with chunked playlists"
```

---

## Phase 6 — Web app skeleton

### Task 20: FastAPI app entrypoint + lifespan

**Files:**
- Create: `local2spoti/main.py`
- Create: `local2spoti/state.py` (shared app state)
- Create: `tests/test_main.py`

- [ ] **Step 1: Failing test**

`tests/test_main.py`:
```python
from httpx import AsyncClient, ASGITransport
import pytest

from local2spoti.main import create_app


async def test_root_redirects_to_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/", follow_redirects=False)
    assert r.status_code in (200, 307)


async def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_main.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement state + main**

`local2spoti/state.py`:
```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from .config import Settings
from .events import EventBus
from .ratelimit import TokenBucket


@dataclass
class AppState:
    settings: Settings
    db_conn: aiosqlite.Connection | None = None
    bus: EventBus = field(default_factory=lambda: EventBus(min_interval=0.1))
    spotify_bucket: TokenBucket = field(
        default_factory=lambda: TokenBucket(rate=3.0, capacity=30.0)
    )
    scan_task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
```

`local2spoti/main.py`:
```python
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_settings
from .db import connect, init_schema
from .state import AppState

try:
    import uvloop
    _HAS_UVLOOP = True
except ImportError:
    _HAS_UVLOOP = False


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.ensure_dirs()
    state = AppState(settings=settings)

    async with connect(settings.db_path) as conn:
        await init_schema(conn)
        state.db_conn = conn
        app.state.app_state = state
        yield


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    app = FastAPI(title="Local2Spoti", lifespan=lifespan)

    static = _static_dir()
    if static.exists():
        app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/")
    async def root():
        return RedirectResponse("/dashboard", status_code=307)

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    @app.get("/dashboard")
    async def dashboard():
        # Filled in by routes/ui.py later
        return JSONResponse({"placeholder": "dashboard"})

    return app


def run() -> None:
    import uvicorn
    if _HAS_UVLOOP:
        uvloop.install()
    settings = load_settings()
    uvicorn.run(
        "local2spoti.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/main.py local2spoti/state.py tests/test_main.py
git commit -m "feat: FastAPI app entrypoint + lifespan"
```

---

### Task 21: Templates base + dashboard

**Files:**
- Create: `local2spoti/templates/base.html`
- Create: `local2spoti/templates/dashboard.html`
- Create: `local2spoti/routes/__init__.py`
- Create: `local2spoti/routes/ui.py`
- Modify: `local2spoti/main.py`
- Create: `tests/test_routes_ui.py`

- [ ] **Step 1: Write base template**

`local2spoti/templates/base.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Local2Spoti{% endblock %}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@2.0.3"></script>
  <script src="https://unpkg.com/htmx.org@2.0.3/dist/ext/ws.js"></script>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen">
  <header class="border-b border-zinc-800 px-6 py-4 flex justify-between items-center">
    <a href="/dashboard" class="text-xl font-semibold">Local2Spoti</a>
    <nav class="flex gap-4 text-sm">
      <a href="/dashboard" class="hover:text-white">Dashboard</a>
      <a href="/scan" class="hover:text-white">Scan</a>
      <a href="/files" class="hover:text-white">Files</a>
      <a href="/review" class="hover:text-white">Review</a>
      <a href="/unmatched" class="hover:text-white">Unmatched</a>
    </nav>
  </header>
  <main class="max-w-6xl mx-auto p-6">{% block content %}{% endblock %}</main>
</body>
</html>
```

- [ ] **Step 2: Write dashboard template**

`local2spoti/templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block title %}Dashboard - Local2Spoti{% endblock %}
{% block content %}
<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
  <section class="bg-zinc-900 rounded-lg p-5">
    <h2 class="text-lg font-semibold mb-3">Library</h2>
    <p class="text-sm text-zinc-400">Root: <code>{{ library_root or "(not set)" }}</code></p>
    <p class="text-sm text-zinc-400">Files in DB: {{ total_files }}</p>
    <p class="text-sm text-zinc-400">Last scan: {{ last_scan_at or "never" }}</p>
  </section>
  <section class="bg-zinc-900 rounded-lg p-5">
    <h2 class="text-lg font-semibold mb-3">Spotify</h2>
    {% if spotify_user %}
      <p class="text-sm">Connected as <strong>{{ spotify_user }}</strong></p>
    {% else %}
      <a href="/auth/login" class="inline-block mt-2 px-4 py-2 bg-emerald-600 rounded">Connect Spotify</a>
    {% endif %}
  </section>
  <section class="bg-zinc-900 rounded-lg p-5 md:col-span-2">
    <h2 class="text-lg font-semibold mb-3">Status</h2>
    <div class="grid grid-cols-4 gap-4 text-center">
      <a href="/files?status=matched" class="bg-zinc-800 rounded p-3">
        <div class="text-2xl">{{ counts.matched or 0 }}</div>
        <div class="text-xs text-zinc-400">Matched</div>
      </a>
      <a href="/review" class="bg-zinc-800 rounded p-3">
        <div class="text-2xl">{{ counts.review or 0 }}</div>
        <div class="text-xs text-zinc-400">Review</div>
      </a>
      <a href="/unmatched" class="bg-zinc-800 rounded p-3">
        <div class="text-2xl">{{ counts.unmatched or 0 }}</div>
        <div class="text-xs text-zinc-400">Unmatched</div>
      </a>
      <a href="/files?status=error" class="bg-zinc-800 rounded p-3">
        <div class="text-2xl">{{ counts.error or 0 }}</div>
        <div class="text-xs text-zinc-400">Errors</div>
      </a>
    </div>
  </section>
  <section class="bg-zinc-900 rounded-lg p-5 md:col-span-2">
    <h2 class="text-lg font-semibold mb-3">Threshold</h2>
    <form hx-post="/api/threshold" hx-trigger="change" class="flex gap-4">
      {% for t in ["strict", "balanced", "loose"] %}
      <label class="flex items-center gap-2">
        <input type="radio" name="threshold" value="{{ t }}" {% if threshold == t %}checked{% endif %}>
        <span class="capitalize">{{ t }}</span>
      </label>
      {% endfor %}
    </form>
    <form action="/api/scan/start" method="post" class="mt-4">
      <button class="px-4 py-2 bg-blue-600 rounded" {% if not spotify_user %}disabled{% endif %}>
        Start scan
      </button>
    </form>
  </section>
</div>
{% endblock %}
```

- [ ] **Step 3: Write ui route**

`local2spoti/routes/__init__.py`: (empty)

`local2spoti/routes/ui.py`:
```python
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import repo
from ..models import FileStatus

router = APIRouter()


def _templates() -> Jinja2Templates:
    tmpl_dir = Path(__file__).parent.parent / "templates"
    return Jinja2Templates(directory=str(tmpl_dir))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    state = request.app.state.app_state
    counts = await repo.count_by_status(state.db_conn)
    cur = await state.db_conn.execute("SELECT COUNT(*) FROM local_file")
    (total_files,) = await cur.fetchone()
    cur = await state.db_conn.execute(
        "SELECT MAX(finished_at) FROM scan_run WHERE status='completed'"
    )
    (last_scan_at,) = await cur.fetchone()
    user_row = await (await state.db_conn.execute(
        "SELECT user_id FROM auth_token WHERE key='spotify'"
    )).fetchone()
    return _templates().TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "library_root": str(state.settings.library_root) if state.settings.library_root else None,
            "total_files": total_files,
            "last_scan_at": last_scan_at,
            "spotify_user": user_row[0] if user_row else None,
            "counts": {k.value: v for k, v in counts.items()},
            "threshold": state.settings.threshold,
        },
    )
```

- [ ] **Step 4: Wire router into main**

Edit `local2spoti/main.py` — replace the placeholder `@app.get("/dashboard")` with router include:

```python
from .routes.ui import router as ui_router
```

In `create_app()`, after the `@app.get("/health")` block, replace the `@app.get("/dashboard")` placeholder with:
```python
    app.include_router(ui_router)
```

- [ ] **Step 5: Test**

`tests/test_routes_ui.py`:
```python
from httpx import AsyncClient, ASGITransport
import pytest
from bs4 import BeautifulSoup

from local2spoti.main import create_app


async def test_dashboard_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/dashboard")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.find("h2", string=lambda s: s and "Library" in s)
    assert soup.find("h2", string=lambda s: s and "Spotify" in s)
```

Run: `pytest tests/test_routes_ui.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add local2spoti/templates/ local2spoti/routes/ local2spoti/main.py tests/test_routes_ui.py
git commit -m "feat: dashboard template + ui router"
```

---

### Task 22: Files page (filtered list with HTMX pagination)

**Files:**
- Create: `local2spoti/templates/files.html`
- Modify: `local2spoti/routes/ui.py`
- Modify: `tests/test_routes_ui.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_routes_ui.py`:
```python
from datetime import UTC, datetime
from local2spoti.db import connect, init_schema
from local2spoti.models import FileStatus, LocalFile
from local2spoti import repo


async def test_files_page_lists_matched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pre-seed DB
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        await repo.upsert_local_file(conn, LocalFile(
            path="/a.mp3", mtime=1, size=1, format="mp3",
            artist="Daft Punk", title="X", status=FileStatus.MATCHED,
            spotify_track_id="t1",
        ), now=datetime(2026,5,4, tzinfo=UTC))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/files?status=matched")
    assert r.status_code == 200
    assert "Daft Punk" in r.text
```

- [ ] **Step 2: Write template**

`local2spoti/templates/files.html`:
```html
{% extends "base.html" %}
{% block title %}Files - Local2Spoti{% endblock %}
{% block content %}
<div class="mb-4 flex gap-2 text-sm">
  {% for s in statuses %}
  <a href="/files?status={{ s }}"
     class="px-3 py-1 rounded {% if s == status %}bg-blue-600{% else %}bg-zinc-800 hover:bg-zinc-700{% endif %}">
    {{ s }}
  </a>
  {% endfor %}
</div>
<table class="w-full text-sm border-collapse">
  <thead class="text-left text-zinc-400 border-b border-zinc-800">
    <tr>
      <th class="py-2 pr-4">Artist</th>
      <th class="pr-4">Title</th>
      <th class="pr-4">Album</th>
      <th class="pr-4">Format</th>
      <th class="pr-4">Confidence</th>
      <th></th>
    </tr>
  </thead>
  <tbody>
    {% for f in files %}
    <tr class="border-b border-zinc-900">
      <td class="py-1 pr-4">{{ f.artist or "—" }}</td>
      <td class="pr-4">{{ f.title or "—" }}</td>
      <td class="pr-4">{{ f.album or "" }}</td>
      <td class="pr-4 uppercase text-zinc-500">{{ f.format }}</td>
      <td class="pr-4">{% if f.match_confidence %}{{ "%.2f" | format(f.match_confidence) }}{% endif %}</td>
      <td>
        {% if f.spotify_track_id %}
          <a class="text-emerald-400" href="https://open.spotify.com/track/{{ f.spotify_track_id }}" target="_blank">↗</a>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% if files | length == limit %}
<button hx-get="/files?status={{ status }}&offset={{ offset + limit }}"
        hx-target="closest tbody" hx-swap="beforeend"
        class="mt-4 px-3 py-1 bg-zinc-800 rounded">Load more</button>
{% endif %}
{% endblock %}
```

- [ ] **Step 3: Append route**

Append to `local2spoti/routes/ui.py`:
```python
from fastapi import Query


@router.get("/files", response_class=HTMLResponse)
async def files(
    request: Request,
    status: str = Query("matched"),
    offset: int = 0,
    limit: int = 100,
) -> HTMLResponse:
    state = request.app.state.app_state
    try:
        st = FileStatus(status)
    except ValueError:
        st = FileStatus.MATCHED
    files = await repo.list_files_by_status(state.db_conn, st, limit=limit, offset=offset)
    return _templates().TemplateResponse(
        "files.html",
        {
            "request": request,
            "files": files,
            "status": status,
            "statuses": [s.value for s in FileStatus],
            "offset": offset,
            "limit": limit,
        },
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_routes_ui.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/templates/files.html local2spoti/routes/ui.py tests/test_routes_ui.py
git commit -m "feat: files page with status filter + pagination"
```

---

### Task 23: Review queue page + bulk approve API

**Files:**
- Create: `local2spoti/templates/review.html`
- Modify: `local2spoti/routes/ui.py`
- Create: `local2spoti/routes/api.py`
- Modify: `local2spoti/main.py`
- Create: `tests/test_routes_api.py`

- [ ] **Step 1: Failing tests**

`tests/test_routes_api.py`:
```python
from datetime import UTC, datetime
from httpx import AsyncClient, ASGITransport
import pytest

from local2spoti.db import connect, init_schema
from local2spoti.main import create_app
from local2spoti.models import FileStatus, LocalFile, MatchCandidate
from local2spoti import repo


async def _seed_review(tmp_path):
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        for i in range(2):
            await repo.upsert_local_file(conn, LocalFile(
                path=f"/{i}.mp3", mtime=1, size=1, format="mp3",
                artist="Daft Punk", title=f"Track {i}",
                status=FileStatus.REVIEW,
            ), now=now)
            cur = await conn.execute("SELECT id FROM local_file WHERE path=?", (f"/{i}.mp3",))
            (fid,) = await cur.fetchone()
            await repo.insert_candidates(conn, fid, [
                MatchCandidate(spotify_track_id=f"top{i}", spotify_artist="Daft Punk",
                               spotify_title=f"Track {i}", artist_similarity=0.93,
                               title_similarity=0.93, confidence=0.92, rank=1),
                MatchCandidate(spotify_track_id=f"alt{i}", spotify_artist="Daft Punk",
                               spotify_title=f"Track {i} (Live)", artist_similarity=0.93,
                               title_similarity=0.85, confidence=0.85, rank=2),
            ], now=now)


async def test_review_lists_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    await _seed_review(tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/review")
    assert r.status_code == 200
    assert "Track 0" in r.text
    assert "Track 1" in r.text


async def test_bulk_approve_top(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    await _seed_review(tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/review/approve_top_visible", data={"file_ids": "1,2"})
    assert r.status_code == 200
    db = tmp_path / ".local2spoti" / "state.db"
    async with connect(db) as conn:
        cur = await conn.execute("SELECT status, spotify_track_id FROM local_file WHERE id=1")
        st, tid = await cur.fetchone()
    assert st == "matched"
    assert tid == "top0"
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_routes_api.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement review page**

Append to `local2spoti/routes/ui.py`:
```python
@router.get("/review", response_class=HTMLResponse)
async def review(request: Request, offset: int = 0, limit: int = 50) -> HTMLResponse:
    state = request.app.state.app_state
    files = await repo.list_files_by_status(state.db_conn, FileStatus.REVIEW,
                                             limit=limit, offset=offset)
    cards = []
    for f in files:
        cur = await state.db_conn.execute(
            """SELECT spotify_track_id, spotify_artist, spotify_title, spotify_album,
                      confidence, artist_similarity, title_similarity, rank
               FROM match_candidate WHERE local_file_id=? ORDER BY rank LIMIT 5""",
            (f.id,),
        )
        candidates = [
            {"spotify_track_id": r[0], "artist": r[1], "title": r[2], "album": r[3],
             "confidence": r[4], "artist_sim": r[5], "title_sim": r[6], "rank": r[7]}
            for r in await cur.fetchall()
        ]
        cards.append({"file": f, "candidates": candidates})
    return _templates().TemplateResponse(
        "review.html", {"request": request, "cards": cards, "offset": offset, "limit": limit},
    )


@router.get("/unmatched", response_class=HTMLResponse)
async def unmatched(request: Request, offset: int = 0, limit: int = 100) -> HTMLResponse:
    state = request.app.state.app_state
    files = await repo.list_files_by_status(state.db_conn, FileStatus.UNMATCHED,
                                             limit=limit, offset=offset)
    return _templates().TemplateResponse(
        "files.html",
        {"request": request, "files": files, "status": "unmatched",
         "statuses": [s.value for s in FileStatus], "offset": offset, "limit": limit},
    )
```

- [ ] **Step 4: Write review template**

`local2spoti/templates/review.html`:
```html
{% extends "base.html" %}
{% block title %}Review - Local2Spoti{% endblock %}
{% block content %}
<div class="mb-4 flex gap-2">
  <button id="bulk-approve"
          hx-post="/api/review/approve_top_visible"
          hx-include="#review-grid input[name='file_ids']"
          hx-swap="none"
          class="px-3 py-2 bg-emerald-600 rounded">
    Approve top candidate for all visible
  </button>
</div>
<form id="review-grid" class="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
  {% for card in cards %}
  <div class="bg-zinc-900 rounded-lg p-4 space-y-3" id="card-{{ card.file.id }}">
    <input type="hidden" name="file_ids" value="{{ card.file.id }}">
    <div>
      <div class="text-xs text-zinc-400">Local</div>
      <div class="font-semibold">{{ card.file.artist }} — {{ card.file.title }}</div>
      <div class="text-xs text-zinc-500">{{ card.file.album or "" }}</div>
    </div>
    {% if card.candidates %}
    <div class="space-y-1">
      {% for c in card.candidates %}
      <label class="flex items-start gap-2 text-sm">
        <input type="radio" name="cand-{{ card.file.id }}" value="{{ c.spotify_track_id }}"
               {% if c.rank == 1 %}checked{% endif %}>
        <span>
          {{ c.artist }} — {{ c.title }}
          <span class="text-zinc-500">({{ "%.0f%%" | format(c.confidence * 100) }})</span>
        </span>
      </label>
      {% endfor %}
    </div>
    <div class="flex gap-2">
      <button hx-post="/api/review/approve"
              hx-vals='js:{file_id: {{ card.file.id }}, track_id: document.querySelector("input[name=cand-{{ card.file.id }}]:checked").value}'
              hx-target="#card-{{ card.file.id }}" hx-swap="outerHTML"
              class="px-2 py-1 bg-emerald-600 rounded text-xs">Approve</button>
      <button hx-post="/api/review/skip"
              hx-vals='js:{file_id: {{ card.file.id }}}'
              hx-target="#card-{{ card.file.id }}" hx-swap="outerHTML"
              class="px-2 py-1 bg-zinc-700 rounded text-xs">Skip</button>
    </div>
    {% endif %}
  </div>
  {% endfor %}
</form>
{% endblock %}
```

- [ ] **Step 5: Implement api router**

`local2spoti/routes/api.py`:
```python
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .. import repo
from ..models import FileStatus

router = APIRouter(prefix="/api")


@router.post("/review/approve")
async def approve(request: Request, file_id: int = Form(...), track_id: str = Form(...)) -> JSONResponse:
    state = request.app.state.app_state
    await repo.update_match(state.db_conn, file_id,
                             spotify_track_id=track_id, confidence=1.0, method="manual")
    return JSONResponse({"ok": True})


@router.post("/review/skip")
async def skip(request: Request, file_id: int = Form(...)) -> JSONResponse:
    state = request.app.state.app_state
    await repo.set_status(state.db_conn, file_id, FileStatus.UNMATCHED)
    return JSONResponse({"ok": True})


@router.post("/review/approve_top_visible")
async def approve_top_visible(request: Request) -> JSONResponse:
    """Bulk-approve: takes file_ids from form (multiple values allowed),
    sets each to its top-ranked candidate."""
    form = await request.form()
    raw_ids = form.getlist("file_ids")
    ids: list[int] = []
    for v in raw_ids:
        ids.extend(int(x) for x in v.split(",") if x.strip())
    state = request.app.state.app_state
    if not ids:
        return JSONResponse({"approved": 0})
    placeholders = ",".join("?" * len(ids))
    cur = await state.db_conn.execute(
        f"""SELECT mc.local_file_id, mc.spotify_track_id, mc.confidence
            FROM match_candidate mc
            WHERE mc.rank = 1 AND mc.local_file_id IN ({placeholders})""",
        ids,
    )
    rows = await cur.fetchall()
    for fid, track_id, conf in rows:
        await repo.update_match(state.db_conn, fid,
                                 spotify_track_id=track_id, confidence=conf, method="manual")
    return JSONResponse({"approved": len(rows)})


@router.post("/threshold")
async def set_threshold(request: Request, threshold: str = Form(...)) -> JSONResponse:
    if threshold not in ("strict", "balanced", "loose"):
        return JSONResponse({"error": "invalid"}, status_code=400)
    state = request.app.state.app_state
    state.settings.threshold = threshold  # type: ignore[assignment]
    await state.db_conn.execute(
        "INSERT OR REPLACE INTO setting (key, value) VALUES ('threshold', ?)",
        (threshold,),
    )
    await state.db_conn.commit()
    return JSONResponse({"ok": True, "threshold": threshold})
```

- [ ] **Step 6: Wire router**

In `local2spoti/main.py`, add to imports:
```python
from .routes.api import router as api_router
```
In `create_app()`, alongside the existing `app.include_router(ui_router)`:
```python
    app.include_router(api_router)
```

- [ ] **Step 7: Run**

Run: `pytest tests/test_routes_api.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add local2spoti/templates/review.html local2spoti/routes/ui.py local2spoti/routes/api.py local2spoti/main.py tests/test_routes_api.py
git commit -m "feat: review queue page + bulk-approve API"
```

---

### Task 24: Scan trigger + cancel endpoints + scan progress page

**Files:**
- Create: `local2spoti/templates/scan.html`
- Modify: `local2spoti/routes/api.py`
- Modify: `local2spoti/routes/ui.py`
- Create: `local2spoti/routes/ws.py`
- Modify: `local2spoti/main.py`

- [ ] **Step 1: Add ws router**

`local2spoti/routes/ws.py`:
```python
from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/progress")
async def progress(ws: WebSocket) -> None:
    await ws.accept()
    state = ws.app.state.app_state
    queue = await state.bus.subscribe()
    try:
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps(asdict(event)))
    except WebSocketDisconnect:
        pass
    finally:
        await state.bus.unsubscribe(queue)
```

- [ ] **Step 2: Add scan trigger to api router**

Append to `local2spoti/routes/api.py`:
```python
import asyncio
from pathlib import Path

from ..artist_match import match_artist_group, match_per_track  # noqa: F401
from ..matcher import Threshold
from ..pipeline import run_scan
from ..spotify_client import SpotifyClient


@router.post("/scan/start")
async def scan_start(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    if state.scan_task and not state.scan_task.done():
        return JSONResponse({"error": "scan already running"}, status_code=409)
    if not state.settings.library_root:
        return JSONResponse({"error": "library_root not configured"}, status_code=400)

    cur = await state.db_conn.execute(
        "SELECT access_token FROM auth_token WHERE key='spotify'"
    )
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    access_token = row[0]

    threshold = Threshold(state.settings.threshold)
    client = SpotifyClient(access_token=access_token, bucket=state.spotify_bucket)
    state.cancel_event.clear()

    async def _run() -> None:
        try:
            await run_scan(
                conn=state.db_conn, client=client,
                library_root=Path(state.settings.library_root),
                threshold=threshold, bus=state.bus,
            )
        finally:
            await client.aclose()

    state.scan_task = asyncio.create_task(_run())
    return JSONResponse({"ok": True})


@router.post("/scan/cancel")
async def scan_cancel(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    if state.scan_task and not state.scan_task.done():
        state.cancel_event.set()
        state.scan_task.cancel()
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "no scan running"}, status_code=400)
```

- [ ] **Step 3: Scan template**

`local2spoti/templates/scan.html`:
```html
{% extends "base.html" %}
{% block title %}Scan - Local2Spoti{% endblock %}
{% block content %}
<div class="space-y-4" hx-ext="ws" ws-connect="/ws/progress">
  <h1 class="text-xl font-semibold">Scan</h1>
  <div id="stage" class="text-sm text-zinc-400">Waiting…</div>
  <div class="bg-zinc-900 rounded-lg overflow-hidden">
    <div id="bar" class="bg-emerald-600 h-3" style="width:0"></div>
  </div>
  <div id="counts" class="grid grid-cols-4 gap-2 text-center text-sm">
    <div class="bg-zinc-800 rounded p-2"><div id="c-matched">0</div><div class="text-xs text-zinc-400">matched</div></div>
    <div class="bg-zinc-800 rounded p-2"><div id="c-review">0</div><div class="text-xs text-zinc-400">review</div></div>
    <div class="bg-zinc-800 rounded p-2"><div id="c-unmatched">0</div><div class="text-xs text-zinc-400">unmatched</div></div>
    <div class="bg-zinc-800 rounded p-2"><div id="c-errors">0</div><div class="text-xs text-zinc-400">errors</div></div>
  </div>
  <form action="/api/scan/cancel" method="post"><button class="mt-4 px-3 py-1 bg-red-700 rounded">Cancel</button></form>
</div>
<script>
  document.body.addEventListener("htmx:wsAfterMessage", (evt) => {
    const e = JSON.parse(evt.detail.message);
    document.getElementById("stage").textContent = `${e.stage}: ${e.processed}/${e.total}`;
    if (e.total > 0) {
      document.getElementById("bar").style.width = `${Math.round(100 * e.processed / e.total)}%`;
    }
    if (e.matched !== undefined) document.getElementById("c-matched").textContent = e.matched;
    if (e.review !== undefined) document.getElementById("c-review").textContent = e.review;
    if (e.unmatched !== undefined) document.getElementById("c-unmatched").textContent = e.unmatched;
    if (e.errors !== undefined) document.getElementById("c-errors").textContent = e.errors;
  });
</script>
{% endblock %}
```

- [ ] **Step 4: Append scan route**

Append to `local2spoti/routes/ui.py`:
```python
@router.get("/scan", response_class=HTMLResponse)
async def scan(request: Request) -> HTMLResponse:
    return _templates().TemplateResponse("scan.html", {"request": request})
```

- [ ] **Step 5: Wire ws router**

In `local2spoti/main.py`:
```python
from .routes.ws import router as ws_router
```
And in `create_app()`:
```python
    app.include_router(ws_router)
```

- [ ] **Step 6: Manual smoke**

Run: `python -m local2spoti`
Open `http://127.0.0.1:8000`. Confirm dashboard renders. Stop.

- [ ] **Step 7: Commit**

```bash
git add local2spoti/templates/scan.html local2spoti/routes/api.py local2spoti/routes/ui.py local2spoti/routes/ws.py local2spoti/main.py
git commit -m "feat: scan trigger + cancel + WebSocket progress page"
```

---

### Task 25: Spotify OAuth callback routes

**Files:**
- Modify: `local2spoti/routes/api.py`
- Modify: `local2spoti/main.py`
- Create: `tests/test_oauth_routes.py`

- [ ] **Step 1: Failing test**

`tests/test_oauth_routes.py`:
```python
from httpx import AsyncClient, ASGITransport
import pytest

from local2spoti.main import create_app


async def test_login_redirects_to_spotify(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL2SPOTI_SPOTIFY_CLIENT_ID", "test_client_id")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "accounts.spotify.com/authorize" in r.headers["location"]
    assert "client_id=test_client_id" in r.headers["location"]
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_oauth_routes.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement auth routes**

Append to `local2spoti/routes/api.py`:
```python
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse

from ..spotify_oauth import (
    DEFAULT_SCOPE, PKCE, build_authorize_url, exchange_code,
)

# Module-level pkce store keyed by state token (one user, in-memory)
_PKCE_STORE: dict[str, PKCE] = {}


@router.get("/auth/login")
async def auth_login(request: Request) -> RedirectResponse:
    state = request.app.state.app_state
    if not state.settings.spotify_client_id:
        return JSONResponse(
            {"error": "spotify_client_id not configured. Set LOCAL2SPOTI_SPOTIFY_CLIENT_ID."},
            status_code=400,
        )
    pkce = PKCE.generate()
    state_token = secrets.token_urlsafe(16)
    _PKCE_STORE[state_token] = pkce
    redirect_uri = f"http://127.0.0.1:{state.settings.port}/callback"
    url = build_authorize_url(
        client_id=state.settings.spotify_client_id,
        redirect_uri=redirect_uri,
        scope=DEFAULT_SCOPE,
        state=state_token,
        pkce=pkce,
    )
    return RedirectResponse(url, status_code=307)


@router.get("/callback")
async def auth_callback(request: Request) -> RedirectResponse:
    code = request.query_params.get("code")
    state_token = request.query_params.get("state")
    if not code or not state_token or state_token not in _PKCE_STORE:
        return JSONResponse({"error": "invalid callback"}, status_code=400)
    pkce = _PKCE_STORE.pop(state_token)
    state = request.app.state.app_state
    redirect_uri = f"http://127.0.0.1:{state.settings.port}/callback"
    tokens = await exchange_code(
        code=code,
        client_id=state.settings.spotify_client_id,
        redirect_uri=redirect_uri,
        pkce=pkce,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens["expires_in"] - 60)
    # Fetch /me to get user_id
    from ..spotify_client import SpotifyClient
    client = SpotifyClient(access_token=tokens["access_token"], bucket=state.spotify_bucket)
    try:
        me = await client.me()
    finally:
        await client.aclose()
    await state.db_conn.execute(
        """INSERT OR REPLACE INTO auth_token (key, access_token, refresh_token,
                                              expires_at, scope, user_id)
           VALUES ('spotify', ?, ?, ?, ?, ?)""",
        (tokens["access_token"], tokens["refresh_token"],
         expires_at.isoformat(), tokens["scope"], me["id"]),
    )
    await state.db_conn.commit()
    return RedirectResponse("/dashboard", status_code=307)
```

- [ ] **Step 4: Wire (no change needed if api_router already mounted)**

The existing `app.include_router(api_router)` covers these new routes — they just live in the same module without a `/api` prefix. **Move them out of the `/api` prefix** by attaching them to a separate router with no prefix:

Edit `local2spoti/routes/api.py` — add a second router at the top of the file:
```python
auth_router = APIRouter()
```

Change the auth_login and auth_callback decorators from `@router.get` to `@auth_router.get`.

In `local2spoti/main.py`:
```python
from .routes.api import router as api_router, auth_router
```
And in `create_app()`:
```python
    app.include_router(api_router)
    app.include_router(auth_router)
```

- [ ] **Step 5: Run**

Run: `pytest tests/test_oauth_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add local2spoti/routes/api.py local2spoti/main.py tests/test_oauth_routes.py
git commit -m "feat: Spotify OAuth login + callback routes"
```

---

## Phase 7 — Push to Spotify endpoint + AcoustID

### Task 26: "Push matched" endpoint

**Files:**
- Modify: `local2spoti/routes/api.py`
- Create: `tests/test_push_endpoint.py`

- [ ] **Step 1: Failing test**

`tests/test_push_endpoint.py`:
```python
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
import pytest

from local2spoti.db import connect, init_schema
from local2spoti.main import create_app
from local2spoti.models import FileStatus, LocalFile
from local2spoti import repo


async def test_push_endpoint_returns_count(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
                                       expires_at, scope, user_id)
               VALUES ('spotify','at','rt','2099-01-01T00:00:00','x','user1')"""
        )
        await conn.commit()
        await repo.upsert_local_file(conn, LocalFile(
            path="/x.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T", spotify_track_id="t1",
            status=FileStatus.MATCHED,
        ), now=datetime(2026, 5, 4, tzinfo=UTC))

    fake_client = AsyncMock()
    fake_client.me.return_value = {"id": "user1"}
    fake_client.create_playlist.return_value = {"id": "p1"}
    fake_client.add_tracks.return_value = None

    with patch("local2spoti.routes.api.SpotifyClient", return_value=fake_client):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/push")
    assert r.status_code == 200
    assert r.json()["added"] == 1
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_push_endpoint.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement endpoint**

Append to `local2spoti/routes/api.py`:
```python
from ..playlist import push_matched_to_spotify


@router.post("/push")
async def push(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    cur = await state.db_conn.execute(
        "SELECT access_token FROM auth_token WHERE key='spotify'"
    )
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    client = SpotifyClient(access_token=row[0], bucket=state.spotify_bucket)
    try:
        result = await push_matched_to_spotify(conn=state.db_conn, client=client)
    finally:
        await client.aclose()
    return JSONResponse({"playlists_created": result.playlists_created, "added": result.added})
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_push_endpoint.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local2spoti/routes/api.py tests/test_push_endpoint.py
git commit -m "feat: push matched tracks to Spotify endpoint"
```

---

### Task 27: AcoustID deep scan (optional path)

**Files:**
- Create: `local2spoti/acoustid.py`
- Create: `tests/test_acoustid.py`
- Modify: `local2spoti/routes/api.py`

- [ ] **Step 1: Failing test**

`tests/test_acoustid.py`:
```python
import shutil
import pytest

from local2spoti.acoustid import fpcalc_available, AcoustidClient
import respx
import httpx


def test_fpcalc_detection():
    assert fpcalc_available() == (shutil.which("fpcalc") is not None)


@respx.mock
async def test_lookup_returns_top_match():
    respx.get("https://api.acoustid.org/v2/lookup").mock(
        return_value=httpx.Response(200, json={
            "status": "ok",
            "results": [{
                "id": "abc",
                "score": 0.99,
                "recordings": [{
                    "id": "rec1",
                    "title": "Around the World",
                    "artists": [{"name": "Daft Punk"}],
                }],
            }],
        })
    )
    client = AcoustidClient(api_key="test")
    md = await client.lookup(fingerprint="FP", duration=423)
    assert md is not None
    assert md.artist == "Daft Punk"
    assert md.title == "Around the World"


@respx.mock
async def test_lookup_no_match():
    respx.get("https://api.acoustid.org/v2/lookup").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    client = AcoustidClient(api_key="test")
    md = await client.lookup(fingerprint="FP", duration=423)
    assert md is None
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_acoustid.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/acoustid.py`:
```python
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


@dataclass(slots=True)
class AcoustidMatch:
    artist: str
    title: str
    score: float


async def fingerprint(path: Path) -> tuple[int, str] | None:
    """Run fpcalc and return (duration_seconds, fingerprint) or None on failure."""
    if not fpcalc_available():
        return None
    proc = await asyncio.create_subprocess_exec(
        "fpcalc", "-json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    import orjson
    data = orjson.loads(out)
    return int(data["duration"]), data["fingerprint"]


class AcoustidClient:
    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, *, fingerprint: str, duration: int) -> AcoustidMatch | None:
        r = await self._http.get(
            "https://api.acoustid.org/v2/lookup",
            params={
                "client": self._api_key,
                "duration": duration,
                "fingerprint": fingerprint,
                "meta": "recordings",
                "format": "json",
            },
        )
        if r.status_code != 200:
            return None
        data = r.json()
        for result in data.get("results", []):
            for rec in result.get("recordings") or []:
                artists = rec.get("artists") or []
                title = rec.get("title")
                if artists and title:
                    return AcoustidMatch(
                        artist=artists[0].get("name", ""),
                        title=title,
                        score=result.get("score", 0.0),
                    )
        return None
```

- [ ] **Step 4: Append deep-scan endpoint**

Append to `local2spoti/routes/api.py`:
```python
from ..acoustid import AcoustidClient, fingerprint, fpcalc_available
from ..artist_match import match_per_track


@router.post("/deep_scan")
async def deep_scan(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    if not fpcalc_available():
        return JSONResponse({"error": "fpcalc not installed"}, status_code=400)
    if not state.settings.acoustid_api_key:
        return JSONResponse({"error": "acoustid_api_key not set"}, status_code=400)

    cur = await state.db_conn.execute(
        "SELECT id, path, duration_ms FROM local_file WHERE status='unmatched' LIMIT 200"
    )
    rows = await cur.fetchall()
    if not rows:
        return JSONResponse({"updated": 0})

    acoustid = AcoustidClient(api_key=state.settings.acoustid_api_key)
    updated = 0
    try:
        for fid, path_str, dur_ms in rows:
            fp = await fingerprint(Path(path_str))
            if fp is None:
                continue
            dur, fingerprint_str = fp
            md = await acoustid.lookup(fingerprint=fingerprint_str, duration=dur)
            if md is None:
                continue
            await state.db_conn.execute(
                """UPDATE local_file SET artist=?, title=?, status='scanned',
                   metadata_source='acoustid' WHERE id=?""",
                (md.artist, md.title, fid),
            )
            await state.db_conn.commit()
            updated += 1
    finally:
        await acoustid.aclose()
    return JSONResponse({"updated": updated})
```

- [ ] **Step 5: Run**

Run: `pytest tests/test_acoustid.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add local2spoti/acoustid.py tests/test_acoustid.py local2spoti/routes/api.py
git commit -m "feat: AcoustID deep scan endpoint"
```

---

## Phase 8 — Polish

### Task 28: Configure-library endpoint

**Files:**
- Modify: `local2spoti/routes/api.py`
- Modify: `local2spoti/templates/dashboard.html`
- Create: `tests/test_library_config.py`

- [ ] **Step 1: Failing test**

`tests/test_library_config.py`:
```python
from httpx import AsyncClient, ASGITransport
import pytest

from local2spoti.main import create_app


async def test_set_library_root_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    library = tmp_path / "library"
    library.mkdir()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/library", data={"path": str(library)})
    assert r.status_code == 200
    assert r.json()["library_root"] == str(library)


async def test_invalid_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/library", data={"path": "/does/not/exist"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_library_config.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `local2spoti/routes/api.py`:
```python
@router.post("/library")
async def set_library(request: Request, path: str = Form(...)) -> JSONResponse:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return JSONResponse({"error": "not a directory"}, status_code=400)
    state = request.app.state.app_state
    state.settings.library_root = p
    await state.db_conn.execute(
        "INSERT OR REPLACE INTO setting (key, value) VALUES ('library_root', ?)",
        (str(p),),
    )
    await state.db_conn.commit()
    return JSONResponse({"library_root": str(p)})
```

- [ ] **Step 4: Add input to dashboard template**

Replace the `<section class="bg-zinc-900 ...">Library</section>` block in `local2spoti/templates/dashboard.html` with:
```html
<section class="bg-zinc-900 rounded-lg p-5">
  <h2 class="text-lg font-semibold mb-3">Library</h2>
  <form hx-post="/api/library" hx-swap="none" class="space-y-2">
    <input type="text" name="path" value="{{ library_root or '' }}"
           placeholder="/path/to/your/music"
           class="w-full bg-zinc-800 px-3 py-2 rounded text-sm">
    <button class="px-3 py-1 bg-zinc-700 rounded text-sm">Save</button>
  </form>
  <p class="text-sm text-zinc-400 mt-2">Files in DB: {{ total_files }}</p>
  <p class="text-sm text-zinc-400">Last scan: {{ last_scan_at or "never" }}</p>
</section>
```

- [ ] **Step 5: Run**

Run: `pytest tests/test_library_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add local2spoti/routes/api.py local2spoti/templates/dashboard.html tests/test_library_config.py
git commit -m "feat: configure library root from dashboard"
```

---

### Task 29: Persist settings on startup + structured logging

**Files:**
- Modify: `local2spoti/main.py`
- Create: `local2spoti/logging_config.py`

- [ ] **Step 1: Logging config**

`local2spoti/logging_config.py`:
```python
from __future__ import annotations

import logging
from pathlib import Path

import structlog


def configure(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "app.log")
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler, logging.StreamHandler()]
    root.setLevel(logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
```

- [ ] **Step 2: Load saved settings on startup**

In `local2spoti/main.py`, expand the `lifespan`:

```python
from .logging_config import configure as configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_dir)
    state = AppState(settings=settings)

    async with connect(settings.db_path) as conn:
        await init_schema(conn)
        # Restore persisted settings
        cur = await conn.execute("SELECT key, value FROM setting")
        for k, v in await cur.fetchall():
            if k == "library_root":
                state.settings.library_root = Path(v)
            elif k == "threshold":
                state.settings.threshold = v  # type: ignore[assignment]
            elif k == "acoustid_api_key":
                state.settings.acoustid_api_key = v
        state.db_conn = conn
        app.state.app_state = state
        yield
```

- [ ] **Step 3: Verify manually**

Run: `python -m local2spoti`
Open dashboard, save a library path, restart, confirm dashboard still shows that path.

- [ ] **Step 4: Commit**

```bash
git add local2spoti/main.py local2spoti/logging_config.py
git commit -m "feat: structured logging + settings persistence"
```

---

### Task 30: README + setup instructions

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Expand README**

`README.md`:
````markdown
# Local2Spoti

Local web app: scan a folder of audio files, identify each track on Spotify via metadata search, and create chunked Spotify playlists for your library.

Designed for libraries with 15,000+ files. Runs entirely on your own machine.

## Features

- Walks any folder of mp3/flac/aac/m4a/ogg/opus files
- Reads tags via mutagen, falls back to filename parsing
- Artist-first matching against Spotify (~3× faster than per-track search at this scale)
- Confidence-scored auto-match + manual review queue with bulk approval
- Optional AcoustID fingerprint deep scan for unmatched files
- Resumable, incremental: subsequent scans only process new/changed files
- Splits libraries above 9000 tracks into multiple alphabetically-keyed playlists

## Requirements

- Python 3.13+
- macOS or Linux (uvloop unavailable on Windows; falls back to asyncio loop)
- A Spotify account
- A registered Spotify Developer application (free; needed for the client ID)
- Optional: `fpcalc` (Chromaprint) for AcoustID deep scan

## Install

```bash
pip install -e ".[dev]"
```

For AcoustID:
```bash
brew install chromaprint   # macOS
# or
sudo apt install libchromaprint-tools  # Debian/Ubuntu
```

## Spotify Developer setup (one-time)

1. Visit https://developer.spotify.com/dashboard and click "Create app".
2. App name: anything (e.g. "Local2Spoti").
3. Redirect URI: `http://127.0.0.1:8000/callback`
4. Save and copy your "Client ID".
5. Set environment variable:
   ```bash
   export LOCAL2SPOTI_SPOTIFY_CLIENT_ID="your_client_id"
   ```
   Or write to `~/.local2spoti/config.toml`:
   ```toml
   spotify_client_id = "your_client_id"
   ```

## Run

```bash
local2spoti
```

Then open http://127.0.0.1:8000.

1. Click "Connect Spotify" on the dashboard.
2. Set your library path.
3. Click "Start scan".

## Configuration

Configuration lives in `~/.local2spoti/config.toml` and via environment variables prefixed `LOCAL2SPOTI_`.

| Key | Description |
|---|---|
| `library_root` | Path to your music folder |
| `spotify_client_id` | From your Spotify Developer app |
| `acoustid_api_key` | (optional) From https://acoustid.org/api-key |
| `threshold` | `strict` / `balanced` (default) / `loose` |
| `host` | Bind host (default `127.0.0.1`) |
| `port` | Port (default `8000`) |

## Development

```bash
pytest                # all tests
ruff check            # lint
ruff format           # format
mypy local2spoti      # type check
```

## Architecture

See [docs/superpowers/specs/2026-05-04-local2spoti-design.md](docs/superpowers/specs/2026-05-04-local2spoti-design.md).
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: expanded README with setup instructions"
```

---

### Task 31: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write workflow**

`.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install ffmpeg + chromaprint
        run: sudo apt-get update && sudo apt-get install -y ffmpeg libchromaprint-tools
      - name: Install
        run: pip install -e ".[dev]"
      - name: Lint
        run: ruff check && ruff format --check
      - name: Type check
        run: mypy local2spoti
      - name: Test
        run: pytest -v
```

- [ ] **Step 2: Run locally to verify all checks pass**

```bash
ruff check
ruff format --check
mypy local2spoti
pytest -v
```

If any fail, fix inline before committing.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "chore: GitHub Actions CI"
```

---

### Task 32: Background token refresher

Spec §11 requires a background task that refreshes Spotify tokens 5 min before expiry; without it, scans fail when access tokens expire mid-run.

**Files:**
- Create: `local2spoti/token_refresh.py`
- Modify: `local2spoti/main.py`
- Create: `tests/test_token_refresh.py`

- [ ] **Step 1: Failing test**

`tests/test_token_refresh.py`:
```python
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
import pytest

from local2spoti.db import connect, init_schema
from local2spoti.token_refresh import refresh_if_expiring


async def test_refreshes_when_within_threshold(tmp_path):
    db = tmp_path / "t.db"
    async with connect(db) as conn:
        await init_schema(conn)
        soon = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
               expires_at, scope, user_id)
               VALUES ('spotify','old','rt',?,'x','u')""",
            (soon,),
        )
        await conn.commit()
        with patch("local2spoti.token_refresh.refresh_token",
                   new=AsyncMock(return_value={
                       "access_token": "new", "expires_in": 3600,
                       "scope": "x", "token_type": "Bearer",
                   })):
            refreshed = await refresh_if_expiring(
                conn=conn, client_id="cid", threshold_seconds=300,
            )
        assert refreshed is True
        cur = await conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
        (tok,) = await cur.fetchone()
        assert tok == "new"


async def test_skips_when_not_expiring(tmp_path):
    db = tmp_path / "t.db"
    async with connect(db) as conn:
        await init_schema(conn)
        far = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
               expires_at, scope, user_id)
               VALUES ('spotify','keep','rt',?,'x','u')""",
            (far,),
        )
        await conn.commit()
        refreshed = await refresh_if_expiring(
            conn=conn, client_id="cid", threshold_seconds=300,
        )
    assert refreshed is False
```

- [ ] **Step 2: Run (fail)**

Run: `pytest tests/test_token_refresh.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`local2spoti/token_refresh.py`:
```python
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite

from .spotify_oauth import refresh_token


async def refresh_if_expiring(
    *,
    conn: aiosqlite.Connection,
    client_id: str,
    threshold_seconds: int = 300,
) -> bool:
    """If the spotify token expires within `threshold_seconds`, refresh it.

    Returns True if a refresh was performed.
    """
    cur = await conn.execute(
        "SELECT refresh_token, expires_at FROM auth_token WHERE key='spotify'"
    )
    row = await cur.fetchone()
    if not row:
        return False
    rtk, expires_at_iso = row
    expires_at = datetime.fromisoformat(expires_at_iso)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at - datetime.now(UTC) > timedelta(seconds=threshold_seconds):
        return False
    new = await refresh_token(refresh=rtk, client_id=client_id)
    new_expires = datetime.now(UTC) + timedelta(seconds=new["expires_in"] - 60)
    new_refresh = new.get("refresh_token", rtk)  # Spotify may rotate it
    await conn.execute(
        """UPDATE auth_token SET access_token=?, refresh_token=?,
           expires_at=?, scope=? WHERE key='spotify'""",
        (new["access_token"], new_refresh, new_expires.isoformat(), new["scope"]),
    )
    await conn.commit()
    return True


async def refresh_loop(
    *,
    conn: aiosqlite.Connection,
    client_id: str,
    interval_seconds: int = 60,
) -> None:
    """Run forever, checking token expiry every `interval_seconds`."""
    while True:
        try:
            if client_id:
                await refresh_if_expiring(conn=conn, client_id=client_id)
        except Exception:
            pass
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: Wire into lifespan**

Edit `local2spoti/main.py` — modify `lifespan` to start the refresh loop:

```python
from .token_refresh import refresh_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_dir)
    state = AppState(settings=settings)

    async with connect(settings.db_path) as conn:
        await init_schema(conn)
        cur = await conn.execute("SELECT key, value FROM setting")
        for k, v in await cur.fetchall():
            if k == "library_root":
                state.settings.library_root = Path(v)
            elif k == "threshold":
                state.settings.threshold = v  # type: ignore[assignment]
            elif k == "acoustid_api_key":
                state.settings.acoustid_api_key = v
        state.db_conn = conn
        app.state.app_state = state

        refresh_task = asyncio.create_task(
            refresh_loop(conn=conn, client_id=settings.spotify_client_id)
        )
        try:
            yield
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except (asyncio.CancelledError, Exception):
                pass
```

Add `import asyncio` at the top of `local2spoti/main.py` if not already imported.

- [ ] **Step 5: Run**

Run: `pytest tests/test_token_refresh.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add local2spoti/token_refresh.py local2spoti/main.py tests/test_token_refresh.py
git commit -m "feat: background Spotify token refresher"
```

---

### Task 33: Final manual smoke test

This task verifies end-to-end behavior on the user's actual machine.

- [ ] **Step 1: Start the app**

Run: `python -m local2spoti`
Expected: server starts on `http://127.0.0.1:8000`, no errors in the terminal.

- [ ] **Step 2: Connect Spotify**

In browser: visit `http://127.0.0.1:8000/dashboard`. Click "Connect Spotify". Authorize. Expected redirect back to dashboard showing your Spotify display name.

- [ ] **Step 3: Set library path**

In dashboard: enter a small test folder path (10–50 files). Click Save.

- [ ] **Step 4: Start scan**

Click "Start scan". Open `/scan` page. Expected: progress bar advances through stages discovery → metadata → match. Counters update live.

- [ ] **Step 5: Review queue**

After scan completes, navigate to `/review`. Expected: any ambiguous matches are listed as cards. Test bulk approve.

- [ ] **Step 6: Push to Spotify**

On dashboard, click "Push matched to Spotify". Confirm via the Spotify web/desktop client that the playlist exists with expected tracks.

- [ ] **Step 7: Re-run scan**

Click "Start scan" again. Expected: completes in seconds, processes 0 new files.

- [ ] **Step 8: Commit a CHANGELOG note**

`CHANGELOG.md`:
```markdown
# Changelog

## 0.1.0 — 2026-05-04
- Initial release.
- Filesystem scanner (mp3, flac, aac, m4a, ogg, opus).
- Spotify artist-first matching with confidence scoring.
- Manual review queue with bulk approve.
- Chunked Spotify playlists (max 9000 tracks per chunk).
- Optional AcoustID deep-scan for unmatched files.
- Resumable, incremental rescans.
```

```bash
git add CHANGELOG.md
git commit -m "docs: add CHANGELOG for 0.1.0"
```

---

## Wrap-up

After all tasks complete:

```bash
pytest -v
ruff check
mypy local2spoti
```

Tag the release:

```bash
git tag v0.1.0
```

The app is now feature-complete per the spec.
