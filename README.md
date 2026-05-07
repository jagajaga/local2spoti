# Local2Spoti

Sync a local music library (mp3/flac/aac/m4a/ogg/opus) to Spotify playlists. Designed for libraries of 10,000+ tracks where Spotify's `/v1/search` rate limits make naïve per-track lookups impractical.

Runs entirely on your own machine — FastAPI + HTMX + SQLite, no cloud.

---

## Why

The naïve approach — for each local file, search Spotify for `artist + title` — works fine for 100 tracks and falls apart at 10,000. Spotify aggressively throttles `/search`, escalates from 429 to 403 ("Spotify is unavailable in this country"), and can put you in an hours-long timeout after a single bad burst.

Local2Spoti layers four match strategies, ordered cheapest-to-most-rate-limited:

1. **AcoustID fingerprinting + MusicBrainz URL relationships** — free APIs, no Spotify call. Identifies the recording, then asks MB for the Spotify URL on file.
2. **Odesli/SongLink fallback** — when MB has Apple/Deezer/Tidal but no Spotify URL, Odesli converts.
3. **MusicBrainz text search** — query MB by artist+title+album, then resolve the MBID. No Spotify `/search` either.
4. **Spotify ISRC index** — `q=isrc:XXX` is deterministic and one-shot; far higher hit rate than fuzzy artist+title search. ISRCs come from MB and from the file's tags.
5. **Spotify fuzzy search** — last resort, the rate-limited path. Artist-grouped to amortize the per-artist albums fetch across many files.

A persistent SQLite cache means re-scans are effectively free. A 9000-track playlist size cap keeps the resulting library Spotify-import-clean.

---

## Features

- 🚀 Walks any folder of `mp3 / flac / aac / m4a / ogg / opus`
- 🏷 Reads tags via mutagen, falls back to filename parsing, falls back to Claude AI
- 🎯 Five-stage match strategy (above) with per-stage progress bars
- 💾 Persistent artist-catalog cache — no `/search` call on re-scans
- 🔁 Resumable / incremental — subsequent scans process only new or changed files
- 👀 Confidence-scored auto-match + manual review queue with bulk approval
- 📋 Splits libraries above 9000 tracks into multiple alphabetically-keyed playlists
- 🛟 Graceful soft-failure under network blips, AcoustID 5xx, Spotify 401/403/429
- 🌑 Real-time WebSocket progress with ETA; works offline (no HDD) for the post-fingerprint match phases
- 🤖 Optional Claude AI rescue for tracks AcoustID can't identify

---

## Quick start

### 1. Prerequisites

- Python 3.13+
- macOS or Linux (uvloop is unavailable on Windows; falls back to asyncio's loop)
- A Spotify account and a free [Spotify Developer app](https://developer.spotify.com/dashboard) for the client ID
- _Optional but recommended_: `fpcalc` (Chromaprint) for AcoustID fingerprint matching
- _Optional_: an [AcoustID API key](https://acoustid.org/api-key) (free) and an Anthropic API key for the AI rescue

### 2. Install

```bash
git clone https://github.com/jagajaga/local2spoti.git
cd local2spoti
pip install -e ".[dev]"

# AcoustID fingerprinter (optional but recommended)
brew install chromaprint                    # macOS
sudo apt install libchromaprint-tools       # Debian/Ubuntu
```

### 3. Spotify Developer app (one-time)

1. Visit <https://developer.spotify.com/dashboard> → **Create app**.
2. Redirect URI: `http://127.0.0.1:8000/callback` (or whatever port you'll run on).
3. Save the **Client ID**.

### 4. Configure

Either via environment variables (a `.env` file in the project root works):

```bash
LOCAL2SPOTI_SPOTIFY_CLIENT_ID=your_client_id_here
LOCAL2SPOTI_ACOUSTID_API_KEY=your_acoustid_key   # optional
ANTHROPIC_API_KEY=your_anthropic_key             # optional, for AI rescue
LOCAL2SPOTI_PORT=8000                            # default 8000
```

…or via `~/.local2spoti/config.toml`:

```toml
library_root      = "/Volumes/My Music"
spotify_client_id = "your_client_id_here"
acoustid_api_key  = "your_acoustid_key"
threshold         = "balanced"
```

A `.env.example` template is in the repo.

### 5. Run

```bash
local2spoti
```

Open <http://127.0.0.1:8000> and follow the dashboard.

---

## Recommended workflow

The dashboard walks you through five steps, top to bottom:

| # | Action | What it does | Spotify `/search` hits? | HDD needed? |
|---|--------|--------------|-------------------------|-------------|
| 1 | **Start scan** | Walk library, read tags, run the full smart pipeline. | Yes (final stage) | Yes |
| 2 | **Match via fingerprint** | AcoustID → MusicBrainz Spotify URL / Odesli / ISRC. | Only ISRC (cheap) | Yes |
| 3 | **Match via MB text** | MB recording search by artist+title+album → MBID → resolve. | Only ISRC (cheap) | No |
| 4 | **Match (Spotify search)** | Last resort: fuzzy `/v1/search` with artist-grouped catalog cache. | Yes, lots | No |
| 5 | **Push to Spotify** | Create chunked playlists (max 9000 tracks each). | A few `/playlists` calls | No |

You can run steps 2–4 concurrently — each lives in its own task slot and uses different rate-limit buckets.

After a scan, **the HDD can be unplugged**: steps 3, 4, 5, AI rescue, and review-queue work all read from the local SQLite DB.

---

## Configuration reference

Environment variables (prefixed `LOCAL2SPOTI_`) and `~/.local2spoti/config.toml` keys merge with environment taking precedence.

| Key | Description | Default |
|---|---|---|
| `library_root` | Path to your music folder | _(unset)_ |
| `spotify_client_id` | From your Spotify Developer app | _(unset)_ |
| `acoustid_api_key` | From <https://acoustid.org/api-key> | _(unset)_ |
| `threshold` | `strict` / `balanced` / `loose` | `balanced` |
| `host` | Bind host | `127.0.0.1` |
| `port` | Bind port | `8000` |
| `data_dir` | DB / logs root | `~/.local2spoti` |

Anthropic AI rescue picks up `ANTHROPIC_API_KEY` automatically (no `LOCAL2SPOTI_` prefix; the SDK reads it directly).

---

## Architecture

```
                  ┌───────────────┐
                  │  FastAPI app  │  (uvicorn + uvloop, WebSocket bus)
                  └───────┬───────┘
                          │
                  ┌───────▼───────┐
                  │ AppState +    │
                  │ aiosqlite WAL │
                  └───────┬───────┘
                          │
        ┌─────────────────┼─────────────────────┐
        │                 │                     │
┌───────▼──────┐   ┌──────▼──────┐   ┌──────────▼──────────┐
│  pipeline.py │   │  recovery.py│   │   playlist push     │
│  (scan flow) │   │  (AcoustID, │   │   (chunked, 9k cap) │
│              │   │   MB-text,  │   │                     │
│              │   │   AI rescue)│   │                     │
└──────────────┘   └─────────────┘   └─────────────────────┘
```

- **`local2spoti/pipeline.py`** — discovery → metadata → match (ISRC pre-pass + artist-grouped fuzzy search).
- **`local2spoti/recovery.py`** — `deep_scan_unmatched` (fingerprint), `match_via_mb_text`, `ai_scan_unmatched`.
- **`local2spoti/spotify_client.py`** — token-refresh-on-401 retry, soft-rate-limit pause for 429/403, configurable token bucket.
- **`local2spoti/musicbrainz.py`** — `resolve_mbid` (URL rels + ISRCs in one round-trip), `search_recording` (text search).
- **`local2spoti/songlink.py`** — Odesli/SongLink cross-platform URL converter.
- **`local2spoti/artist_cache.py`** — SQLite cache of fetched Spotify artist catalogs (30-day TTL on hits, 1-day on misses).
- **`local2spoti/db.py`** — schema + lightweight `ALTER TABLE` migration via `PRAGMA table_info`.

The full design doc lives at [docs/superpowers/specs/2026-05-04-local2spoti-design.md](docs/superpowers/specs/2026-05-04-local2spoti-design.md).

---

## Development

```bash
pytest -q          # 152 tests, ~30s
ruff check         # lint
ruff format        # auto-format
mypy local2spoti   # type check (strict mode)
```

Run one test:

```bash
pytest tests/test_artist_cache.py::test_positive_round_trip -v
```

UI changes are picked up on browser refresh (Jinja2 auto-reloads). Server-side Python changes require a process restart — uvicorn is started without `--reload` to keep long-running scan/match tasks safe.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Spotify is unavailable in this country` 403s | You hit the `/search` soft-rate-limit ceiling | Wait 5 min (auto-paused), or run **Match via MB text** while you wait |
| Match progress is stuck for >2 min | Spotify bucket paused, or a slow fpcalc on a USB drive | Watch the WebSocket health badge — if green, just patience |
| `AcoustID error 4: invalid API key` | Wrong / missing `LOCAL2SPOTI_ACOUSTID_API_KEY` | Get a free key at <https://acoustid.org/api-key> |
| `401 The access token expired` | OAuth token rotated mid-job | Auto-recovers per request; if it sweeps the run, file a bug |
| `fpcalc not installed` | Chromaprint missing | `brew install chromaprint` (or distro equivalent) |
| New ISRC tags don't appear after fingerprint | Browser cached old dashboard | Hard-refresh `/dashboard` |

---

## License

MIT — see [LICENSE](LICENSE).
