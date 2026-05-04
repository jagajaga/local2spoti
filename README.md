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
