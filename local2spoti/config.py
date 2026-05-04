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
