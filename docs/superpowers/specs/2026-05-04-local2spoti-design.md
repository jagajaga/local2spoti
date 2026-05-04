# Local2Spoti — Design Spec

**Date:** 2026-05-04
**Status:** Approved (pending final user review)

## 1. Goal

A local-only web app that scans a folder containing 15,000+ audio files (mp3, flac, aac, m4a, ogg, opus, …), identifies each track on Spotify via metadata search, and creates a set of Spotify playlists mirroring the local library on the user's account.

The app is single-user, runs on the user's own machine, and is optimized for the realistic constraints of the Spotify Web API (rate limits, 10,000-track playlist cap, no audio fingerprinting endpoint).

## 2. Hardware target

The reference machine for performance tuning:

- Apple M1 Max — 8 performance + 2 efficiency cores
- 32 GB RAM
- Apple Fabric NVMe SSD
- macOS 26.4
- Python 3.13 available

The app must run reasonably on lower-spec machines, but defaults are tuned for this hardware.

## 3. Key product decisions

| Decision | Choice |
|---|---|
| Library too large for one playlist (15k > 10k cap) | Split into multiple playlists, chunked alphabetically by artist; chunk size 9,000 to leave headroom for incremental adds |
| Deployment | Local-only, web UI on `localhost`, single user |
| Imperfect matches | Auto-add high-confidence; ambiguous → review queue with bulk "approve top candidate" action; unmatched → reported |
| File metadata source | Tags first (mutagen), filename fallback parsing, AcoustID as on-demand "deep scan" for unmatched only |
| Match threshold | UI slider (Strict / Balanced / Loose); default Balanced |
| Resumability | SQLite-backed state, fully resumable from any interruption |
| Rescan behavior | Incremental — only re-process files whose `(path, mtime, size)` changed; matched files stay matched |
| Local files deleted from disk | Tracks remain in the Spotify playlist (no pruning) |

## 4. Architecture

**Single-process FastAPI app with async background tasks.** One Python process serves the web UI + JSON API and runs the scan/match pipeline as in-process async tasks. SQLite holds all state. WebSocket pushes live progress to the browser.

Rationale: the bottleneck is Spotify API rate limits (~180 req/min), not CPU or disk. Async with bounded concurrency is the right shape; a separate worker process and a queue (Celery/Arq + Redis) would add infrastructure for negligible benefit at single-user scale.

## 5. Tech stack

- **Python 3.13**
- **FastAPI** — web framework, native async, WebSocket support
- **uvicorn** — ASGI server
- **uvloop** — replacement event loop, 2–4x faster than stdlib asyncio for heavy I/O concurrency
- **aiosqlite** — async SQLite driver
- **mutagen** — tag reading for all supported audio formats
- **spotipy** — Spotify OAuth helper (token management)
- **httpx** — direct HTTP client for Spotify, with custom rate limiter
- **orjson** — JSON parsing for large Spotify responses (artist catalogs)
- **rapidfuzz** — fast fuzzy string matching (C++ backed)
- **structlog** — structured logging
- **pyacoustid** + **fpcalc** binary — optional, only for AcoustID deep scan
- **Frontend:** Jinja2 templates + HTMX + a small amount of vanilla JS; Tailwind via CDN. No build step.

## 6. Project layout

```
local2spoti/
├── pyproject.toml
├── README.md
├── local2spoti/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entrypoint
│   ├── config.py            # settings: paths, API keys, thresholds
│   ├── db.py                # SQLite schema + connection helpers
│   ├── models.py            # Pydantic + dataclass models
│   ├── scanner.py           # filesystem walk, tag reading, filename parsing
│   ├── matcher.py           # Spotify search + confidence scoring
│   ├── acoustid.py          # optional fingerprinting
│   ├── playlist.py          # Spotify playlist creation + chunking
│   ├── pipeline.py          # orchestrates scan → match → add, with progress events
│   ├── spotify_client.py    # auth, rate-limited HTTP wrapper
│   ├── routes/
│   │   ├── ui.py            # HTMX-rendering endpoints (HTML responses)
│   │   ├── api.py           # JSON API (start scan, threshold, etc.)
│   │   └── ws.py            # WebSocket for live progress
│   ├── templates/
│   └── static/
└── tests/
```

## 7. Data model (SQLite)

```sql
-- One row per scan invocation. History + per-run diff.
CREATE TABLE scan_run (
  id              INTEGER PRIMARY KEY,
  root_path       TEXT NOT NULL,
  started_at      TEXT NOT NULL,           -- ISO8601 UTC
  finished_at     TEXT,
  status          TEXT NOT NULL,           -- running | completed | failed | cancelled
  threshold       TEXT NOT NULL,           -- strict | balanced | loose (snapshot)
  total_files     INTEGER,
  matched_count   INTEGER,
  review_count    INTEGER,
  unmatched_count INTEGER,
  error_message   TEXT
);

-- One row per local audio file ever seen. Persistent across scans.
-- (path, mtime, size) is the change-detection key for incremental rescans.
CREATE TABLE local_file (
  id              INTEGER PRIMARY KEY,
  path            TEXT NOT NULL UNIQUE,
  mtime           INTEGER NOT NULL,        -- unix seconds
  size            INTEGER NOT NULL,
  format          TEXT NOT NULL,           -- mp3 | flac | aac | m4a | ogg | opus | ...
  duration_ms     INTEGER,

  -- Parsed metadata (from tags, with filename fallback)
  artist          TEXT,
  title           TEXT,
  album           TEXT,
  track_number    INTEGER,
  metadata_source TEXT,                    -- tags | filename | acoustid | manual

  -- Match state
  status           TEXT NOT NULL,          -- new | scanned | matched | review | unmatched | error | missing
  spotify_track_id TEXT,
  match_confidence REAL,                   -- 0.0 - 1.0
  match_method     TEXT,                   -- auto | manual | acoustid

  -- Bookkeeping
  first_seen_at   TEXT NOT NULL,
  last_scanned_at TEXT,
  last_error      TEXT,
  last_run_id     INTEGER REFERENCES scan_run(id)
);
CREATE INDEX idx_local_file_status ON local_file(status);
CREATE INDEX idx_local_file_run    ON local_file(last_run_id);

-- Candidates for review-state files. Top 5 stored.
CREATE TABLE match_candidate (
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
  rank                INTEGER NOT NULL,    -- 1 = top candidate
  fetched_at          TEXT NOT NULL
);
CREATE INDEX idx_match_candidate_file ON match_candidate(local_file_id, rank);

-- Spotify playlists we've created.
CREATE TABLE playlist (
  id                  INTEGER PRIMARY KEY,
  spotify_playlist_id TEXT NOT NULL UNIQUE,
  name                TEXT NOT NULL,        -- e.g. "Local Library 1/2 (A–F)"
  chunk_index         INTEGER NOT NULL,
  alpha_range         TEXT,
  created_at          TEXT NOT NULL,
  track_count         INTEGER NOT NULL DEFAULT 0
);

-- Source of truth for "this file is already in a Spotify playlist".
CREATE TABLE playlist_track (
  playlist_id      INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
  local_file_id    INTEGER NOT NULL REFERENCES local_file(id) ON DELETE CASCADE,
  spotify_track_id TEXT NOT NULL,
  added_at         TEXT NOT NULL,
  PRIMARY KEY (playlist_id, local_file_id)
);

-- OAuth tokens (single row keyed 'spotify')
CREATE TABLE auth_token (
  key           TEXT PRIMARY KEY,
  access_token  TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  scope         TEXT NOT NULL,
  user_id       TEXT
);

-- Key/value config
CREATE TABLE setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

**SQLite tuning** (applied at connection open):

```sql
PRAGMA journal_mode    = WAL;
PRAGMA synchronous     = NORMAL;
PRAGMA cache_size      = -65536;       -- 64 MB
PRAGMA mmap_size       = 268435456;    -- 256 MB
PRAGMA temp_store      = MEMORY;
PRAGMA busy_timeout    = 5000;
PRAGMA foreign_keys    = ON;
```

**Design notes:**

- `local_file` is the source of truth across scans.
- `status` is a state machine: `new → scanned → (matched | review | unmatched | error)`. UI filters by status.
- `match_candidate` is only populated for files in `review` status; auto-matched files store `spotify_track_id` directly on `local_file` (saves ~75k rows of noise).
- Playlist chunks are 9,000, leaving 1,000 headroom for incremental adds before spilling into a new chunk.
- Files removed from disk transition to `status='missing'` but their `playlist_track` rows are kept.

## 8. Pipeline

End-to-end: `folder → filesystem walk → metadata extraction → Spotify search → confidence scoring → decision → playlist assignment → batch upload`. Stages communicate through SQLite; writes are durable, so the pipeline is naturally resumable.

### Stage 1: Discovery & change detection

Single async generator walks the library root with `os.scandir`. For each file matching the audio extension allowlist:

1. Read `(path, mtime, size)`.
2. SELECT existing row by `path`.
3. If no row → INSERT with `status='new'`, yield to next stage.
4. If row exists and `(mtime, size)` unchanged → skip entirely.
5. If row exists but file changed → UPDATE, set `status='new'`, yield.

After the walk completes, any `local_file` row whose `last_scanned_at` is older than the current scan's `started_at` has disappeared from disk. These rows are updated to `status='missing'` and `last_scanned_at = now`. Their `playlist_track` rows are left untouched (per product decision: deleted local files stay in the Spotify playlist).

Walk emits a discovery total early so the UI's progress bar has a denominator within seconds.

### Stage 2: Metadata extraction

Bounded thread pool (16 workers — oversubscribe 8 P-cores 2:1 to overlap I/O with CPU) consuming `status='new'`:

1. `mutagen.File(path, easy=True)` → `artist`, `title`, `album`, `tracknumber`, `length`.
2. **Normalize**: strip `feat.` / `ft.` / `featuring` parens; preserve version qualifiers like `(Remastered 2011)`, `(Radio Edit)` (they help match the right version); unicode NFC; lowercase only for similarity comparisons (display text preserved).
3. **Filename fallback** if tags missing artist or title:
   - Try patterns in order: `{artist} - {title}`, `{tracknum} - {artist} - {title}`, `{tracknum}. {title}` (using folder name for `{artist}`).
   - Fall back to parent / grandparent folder names for `{album}` / `{artist}`.
4. If still no artist+title → `status='unmatched'`, `metadata_source='none'`, reason logged.
5. Otherwise write parsed fields, `status='scanned'`, `metadata_source='tags'|'filename'`.

### Stage 3: Spotify search & scoring (artist-first, optimized)

The major performance optimization: instead of one Spotify search per file (15,000 requests), files are clustered by normalized artist name and each artist's full catalog is fetched once.

Bounded async pool (12 concurrent tasks) iterating artist groups:

1. Group `local_file` rows in `status='scanned'` by normalized artist. A 15k library typically has 1.5k–3k unique artists.
2. For each artist:
   - 1 search call → resolve canonical Spotify artist ID.
   - 1 albums call (paginated 50/page) → list of album IDs.
   - Batched album calls via `/v1/albums?ids=` (up to 20 per call) → full track listings.
3. Match each local file against the in-memory artist catalog with `rapidfuzz`. Score every catalog candidate; keep the top 5.
4. **Confidence score** (0.0–1.0):
   ```
   artist_sim  = rapidfuzz.token_set_ratio(local_artist_norm, spotify_artist_norm) / 100
   title_sim   = rapidfuzz.token_set_ratio(local_title_norm,  spotify_title_norm)  / 100
   album_bonus = 0.05 if albums match (token_set_ratio ≥ 90), else 0
   dur_bonus   = 0.10 if |Δdur| ≤ 3000ms, 0.05 if ≤ 7000ms, else 0
   confidence  = 0.45*artist_sim + 0.45*title_sim + album_bonus + dur_bonus
   ```
5. **Decision** based on selected threshold preset:
   - **strict**: auto if `top.artist_sim ≥ 0.95 AND top.title_sim ≥ 0.95 AND |Δdur| ≤ 3s`
   - **balanced** (default): auto if `top.artist_sim ≥ 0.90 AND top.title_sim ≥ 0.90 AND (album_match OR |Δdur| ≤ 5s)`
   - **loose**: auto if `top.artist_sim ≥ 0.80 AND top.title_sim ≥ 0.80`
6. **Outcomes:**
   - **Auto-match** → `spotify_track_id`, `match_confidence`, `match_method='auto'`, `status='matched'`.
   - **Review** → at least one candidate has `confidence ≥ 0.50` but top didn't pass threshold. Insert top 5 candidates, `status='review'`.
   - **Unmatched** → no candidate ≥ 0.50. `status='unmatched'`. Eligible for AcoustID deep scan.

**Fallback path:** if step 2 returns 0 results for an artist (compilation albums, mistagged artists), the artist's files fall back to per-track qualified search using `track:"{title}" artist:"{artist}"`.

**De-duplication:** `(normalized_artist, normalized_title)` pairs that occur multiple times in the library get one search; the result is fan-out applied to all duplicate files.

### Stage 4: Manual review (event-driven)

Users interact with `status='review'` files in the review-queue UI. Actions:

- Approve a specific candidate → set `spotify_track_id`, `match_method='manual'`, `status='matched'`.
- Bulk "approve top candidate for all visible" → bulk update; default 50 cards per page.
- Skip → `status='unmatched'`.
- Search manually (typed query) → live Spotify search → user picks → `status='matched'`.

### Stage 5: AcoustID deep scan (on-demand)

Triggered by a button on the unmatched view. Iterates `status='unmatched'`:

1. `fpcalc` subprocess → Chromaprint fingerprint (~1–2s per file, CPU-bound).
2. POST to AcoustID API → MusicBrainz recording IDs → resolve to artist + title.
3. Re-run Stage 3 search with the better metadata.
4. Update `metadata_source='acoustid'`. Outcome: matched, review, or stay unmatched.

Concurrency cap 4 fingerprints + 3 AcoustID req/s (their published rate limit).

If `fpcalc` is not installed, the deep-scan button is disabled with an inline `brew install chromaprint` hint. Main pipeline does not depend on `fpcalc`.

### Stage 6: Playlist assignment & upload

Runs after a scan completes, or on demand ("Push matched to Spotify"):

1. SELECT `local_file` where `status='matched'` AND no row in `playlist_track`.
2. Sort by `(artist, album, track_number, title)`.
3. Chunk into groups of 9,000 by alphabetical artist range. Naming: `Local Library 1/N (A–F)`.
4. For each chunk:
   - If a `playlist` row exists for this chunk and has room (< 9,000) → append.
   - Otherwise create a new Spotify playlist via API, insert `playlist` row.
   - Add tracks in batches of 100 (API limit), insert `playlist_track` rows on success.
5. On 429 → respect `Retry-After`, persist progress, continue. `playlist_track` is the resume key.

## 9. Performance optimizations

| # | Optimization | Estimated impact |
|---|---|---|
| 1 | **Artist-first matching** (Stage 3) — ~4,500 calls vs ~15,000 | API portion: ~25 min vs ~85 min (3.4×) |
| 2 | **uvloop** instead of stdlib asyncio | 2–4× on async I/O concurrency |
| 3 | **orjson** for JSON parsing | 2–3× on Spotify response parsing (artist catalogs are large) |
| 4 | **SQLite tuning** (WAL, mmap, 64MB cache) | Most reads zero-disk on 32GB RAM |
| 5 | **Bulk transactions** + `executemany` | ~20× write throughput |
| 6 | **Concurrency right-sized to M1 Max**: 16 tag threads, 12 match tasks, 4 fingerprint procs | Saturates I/O without thrashing |
| 7 | **De-duplicate** identical `(artist, title)` searches | ~5–10% in fallback path |
| 8 | **WebSocket update throttling** (10 Hz max) | UI stays responsive at 15k events |
| 9 | **Shared token-bucket rate limiter** | Hard ceiling at 180 req/min, automatic 429 handling |
| 10 | **Python 3.13** | Free async + GC improvements over 3.11 |

### Estimated total runtime, fresh 15k-file scan, M1 Max + NVMe

| Stage | Time |
|---|---|
| Discovery + change detection | ~10 sec |
| Tag extraction (16 threads) | ~60 sec |
| Spotify match (artist-first) | ~25 min |
| Playlist upload (~12k tracks) | ~3 min |
| **Total** | **~30 min** |

Subsequent rescans of an unchanged library: ~10 sec.

## 10. Web UI

Server-rendered Jinja2 + HTMX. Five pages.

### `/` — Dashboard

- Library card: configured root, total files in DB, last scan timestamp, "Configure library" button.
- Spotify card: connected user or "Connect Spotify" button.
- Status counters: matched / review / unmatched / new — clickable.
- Playlists card: list with track counts and "Open in Spotify" links.
- Primary action: "Start scan" (disabled if Spotify not connected).
- Secondary action: "Push matched to Spotify" (when matched-but-not-pushed > 0).
- Threshold preset radio: Strict / Balanced / Loose.

### `/scan` — Live scan progress

- Stage indicator (discovery → metadata → matching → upload).
- Per-stage progress bar with `processed / total` and ETA.
- Live ticker, last 20 events, coalesced server-side at 10 Hz.
- Live counters via HTMX OOB swaps.
- "Cancel scan" — sets `status='cancelled'`, workers drain on next checkpoint.
- WebSocket-driven.

### `/files` — Filtered file list

- Filter chips: All / Matched / Review / Unmatched / Missing / Errors.
- Free-text search across artist, title, album, path.
- Columns: Path, Artist, Title, Album, Format, Status, Confidence, Spotify link.
- Row expand → tag-vs-Spotify comparison + manual override.
- HTMX pagination, 100/page.

### `/review` — Review queue

Card grid (3 cols on wide screens). Each card:

- Local side: artist / title / album / duration / source folder.
- Top Spotify candidate: artist / title / album / duration with confidence breakdown.
- Inline radio for top 5 candidates; default selected = top.
- Per-card: Approve · Skip · Manual search.

Top-bar bulk actions:
- "Approve top candidate for all visible" — page-scoped (50 cards default).
- "Approve top for all in queue" — full-queue, with confirmation.
- "Skip all visible".

Keyboard shortcuts: `J`/`K` next/prev, `1`–`5` pick candidate, `A` approve, `S` skip.

### `/unmatched` — Unmatched & deep-scan

- Same table as `/files` filtered to unmatched.
- "Deep scan with AcoustID" button — disabled if `fpcalc` missing; first run prompts for AcoustID API key.
- Per-file "search manually" modal.

### Cross-cutting

- Toast notifications for background-task triggers.
- Dark mode default, light toggle in header.
- No login screen; first-run experience is "Connect Spotify".

## 11. Concurrency, rate limits, auth, error handling

### Spotify OAuth

Authorization Code with PKCE. Redirect URI `http://127.0.0.1:8765/callback`. Tokens persisted to `auth_token`. Background refresher swaps tokens 5 min before expiry.

Scopes: `playlist-modify-private playlist-modify-public playlist-read-private user-read-private`. Default playlists private, per-scan toggle for public.

Client ID is user-supplied — README walks through registering a Spotify Developer app (one-time, ~2 min). Avoids embedding a shared client ID that could get rate-limited or revoked.

### Rate limiting

Single shared token-bucket limiter in `spotify_client.py`:
- Capacity 30 tokens, refill 3/sec (~180/min sustained, burst headroom).
- All workers `await bucket.acquire()` before any HTTP call.
- On 429: read `Retry-After`, drain bucket, sleep, retry. Log line surfaces in UI ticker.
- On 5xx: exponential backoff with jitter (1s → 2s → 4s → 8s, max 4 retries). After 4 failures: `status='error'` with stored message.

### Concurrency model

```
asyncio event loop (uvloop)
├── Walk task          (1 producer)
├── Tag thread pool    (16 workers — mutagen, GIL-releasing on file I/O)
├── Match worker pool  (12 async tasks — Spotify search/catalog)
├── Upload worker pool (4 async tasks — playlist add)
├── WebSocket broadcaster (1 task; coalesces events at 10 Hz)
└── HTTP server        (FastAPI request handlers)
```

Bounded asyncio queues between stages → automatic backpressure.

Cancellation: setting `scan_run.status='cancelled'` and signaling a `cancel_event`. Workers finish current item, persist state, exit cleanly. Restart resumes from saved state.

### Error matrix

| Failure | Behavior |
|---|---|
| Corrupt tags on one file | `status='error'`, scan continues |
| Spotify 429 | Retry with `Retry-After`; no file marked failed |
| Spotify 5xx transient | Exponential backoff, up to 4 retries |
| Spotify 5xx persistent | `status='error'`, scan continues |
| Token expired mid-scan | Background refresher; if fails, scan pauses with banner; resumes on reconnect |
| Network down | Pause pipeline, retry every 30s, resume |
| Process killed | All state in SQLite via per-batch transactions; restart resumes |
| Disk full | Pipeline halts, banner surfaces error, no corruption (WAL guarantee) |
| AcoustID API down | Per-file 15s timeout, file stays unmatched, scan continues |
| `fpcalc` missing | Deep-scan button disabled with install hint |

### Logging & observability

- `structlog` JSON logs to `~/.local2spoti/logs/app.log`, plus pretty stderr in dev.
- Per-scan summary on completion: counts, duration per stage, error breakdown. Saved to `scan_run` columns and shown on dashboard.
- Errors view in UI: filtered `status='error'` files with retry button (per-file or bulk).

### Configuration & data locations

```
~/.local2spoti/
├── config.toml         # library root, threshold default, AcoustID key, Spotify client ID
├── state.db            # SQLite — all the data
├── state.db-wal
└── logs/
    ├── app.log
    └── app.log.1       # rotated weekly, 4 weeks retained
```

`config.toml` is hand-editable; UI also writes to it. Sensible defaults if file is missing.

## 12. Testing strategy

### Unit tests (`pytest`, no I/O)

- **Confidence scoring** (table-driven) — most important tests in the project; every threshold-tuning change must keep these green.
- **Filename parsing** — ~30 representative patterns including unicode and weird separators.
- **Normalization** — `feat.` stripping, parens, NFC, lowercase. Frozen golden file.
- **Token-bucket limiter** — deterministic time via `freezegun`; verify capacity, refill, blocking.
- **Playlist chunking** — given N files and existing chunk capacities, verify correct chunk creation and target selection.

### Integration tests (`respx`-mocked Spotify)

- Captured Spotify response fixtures in `tests/fixtures/spotify/` driven by `respx`.
- **End-to-end pipeline test**: 50-file synthetic library with mixed quality; assertions on final state distribution, resumability (cancel mid-run), incremental rescan (0 work on no-change re-run, exactly N work on N new files).

### Database tests

- Schema applies cleanly from empty DB.
- State machine transitions enforced.
- Concurrent reader during writer doesn't deadlock (verifies WAL config).

### Audio fixtures

- ~20 small (few-second) real audio files per supported format in `tests/fixtures/audio/`. Generated at test setup with `ffmpeg` or stored via Git LFS.

### Smoke test (manual, opt-in)

- `make smoke` — 10-file scan against real Spotify dev account. Not in CI. Run before releases.

### Frontend / HTMX

- Server-side route tests via `httpx.AsyncClient` against the running ASGI app, asserting on parsed HTML fragments.
- Playwright tests for critical interactive flows (bulk-approve, keyboard shortcuts, scan progress WS). Run on demand, not in CI.

### Out of scope

- Mutagen internals
- Spotify API correctness
- `fpcalc` correctness
- Visual styling

### CI

GitHub Actions on push:
1. `ruff check` + `ruff format --check`
2. `mypy --strict`
3. `pytest` unit + integration
4. `python -m build` sanity check

Target: 60–90 sec per CI run.
