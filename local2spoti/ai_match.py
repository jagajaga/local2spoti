"""Claude-powered metadata identification for files unmatched by tag/AcoustID search.

For each unmatched file we send the file path + any tag fragments to Claude;
the model returns its best guess at (artist, title, album) along with a
confidence rating. High-confidence guesses are written back as the file's
metadata so the next match pass can find them on Spotify.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import anthropic

# Default model — overridable via the CLAUDE_MODEL env var so users on cheaper
# tiers can swap for Sonnet without touching code.
DEFAULT_MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """You are a music librarian helping identify audio tracks from sparse on-disk metadata.

For each file you'll receive:
- A unique numeric `id`
- The file's absolute path on disk (often the strongest signal)
- Existing tag values for artist / title / album, which may be empty, wrong, or partial

Your job: identify the most likely artist and song title that exists on Spotify.

Rules:
- Be conservative. If the path is unintelligible (random hex, UUID-named, no clue at all), set artist/title to null and confidence to "none".
- Prefer the canonical artist name (e.g. "The Beatles" not "Beatles, The").
- Strip remix/version annotations from the title only if clearly noise; preserve "(Live)", "(Remix)", "(Original Mix)", "(Remastered 2009)" when present.
- Don't invent tracks. If you're not confident the track exists on Spotify under your guessed name, use confidence "low".
- Confidence levels:
  - "high": clearly identifiable (e.g. ".../Daft Punk/Discovery/05 - Aerodynamic.mp3")
  - "medium": probable but ambiguous (typos, partial names, transliteration)
  - "low": guess based on weak signals (e.g. only a track number + folder name)
  - "none": no usable signal — give up
- Always return exactly one entry per input id."""


# Structured-output schema. Strings can be null via anyOf — the JSON-schema
# subset Claude enforces accepts anyOf but not all of JSON Schema's nullable
# shorthands; anyOf is the portable way to express "string or null".
SUGGESTIONS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "artist": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "album": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "none"],
                    },
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "id", "artist", "title", "album", "confidence", "reasoning",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class Suggestion:
    file_id: int
    artist: str | None
    title: str | None
    album: str | None
    confidence: str  # "high" | "medium" | "low" | "none"
    reasoning: str

    @property
    def usable(self) -> bool:
        """Has the model produced an artist+title we can feed back to Spotify."""
        return bool(self.artist) and bool(self.title) and self.confidence != "none"


class AIClient:
    """Thin async wrapper around the Anthropic SDK for batch metadata guessing."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        # AsyncAnthropic auto-reads ANTHROPIC_API_KEY from os.environ when api_key=None.
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)

    async def aclose(self) -> None:
        await self._client.close()

    async def suggest_metadata(self, files: list[dict]) -> list[Suggestion]:
        """Ask Claude to identify each file in `files`.

        Each input dict must have: `id` (int), `path` (str), and optionally
        `artist`/`title`/`album` (str or None) for tag fragments we already have.
        """
        if not files:
            return []

        user_payload = {"files": files}

        message = await self._client.messages.create(
            model=self._model,
            max_tokens=8000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={
                "format": {"type": "json_schema", "schema": SUGGESTIONS_SCHEMA}
            },
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Identify the following audio files. Return one entry per `id`.\n\n"
                        + _stable_json(user_payload)
                    ),
                }
            ],
        )

        text = next((b.text for b in message.content if b.type == "text"), "")
        if not text:
            return []
        import orjson
        data = orjson.loads(text)
        return [
            Suggestion(
                file_id=item["id"],
                artist=item.get("artist"),
                title=item.get("title"),
                album=item.get("album"),
                confidence=item["confidence"],
                reasoning=item.get("reasoning", ""),
            )
            for item in data.get("items", [])
        ]


def _stable_json(obj: object) -> str:
    """JSON encode with sorted keys so the user prompt is deterministic.

    Worth doing even though we only cache the system prompt — keeps test
    fixtures stable and avoids subtle non-determinism.
    """
    import orjson
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2).decode()
