"""Settings from .env plus the committed league.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    league_id: str
    my_team_name: str
    my_team_short: str
    db_path: Path
    league: dict[str, Any] = field(default_factory=dict)

    def require_league_id(self) -> str:
        if not self.league_id:
            raise ConfigError(
                "FANTRAX_LEAGUE_ID is not set. Copy .env.example to .env and fill it in "
                "(it's the id in fantrax.com/fantasy/league/<LEAGUE_ID>/...)."
            )
        return self.league_id


def load_league_yaml(path: Path | None = None) -> dict[str, Any]:
    path = path or DATA_DIR / "league.yaml"
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or REPO_ROOT / ".env")
    db = os.environ.get("HOCKEY_DB") or str(REPO_ROOT / "var" / "hockey.db")
    return Settings(
        league_id=os.environ.get("FANTRAX_LEAGUE_ID", "").strip(),
        my_team_name=os.environ.get("MY_TEAM_NAME", "The Blue Blazers").strip(),
        my_team_short=os.environ.get("MY_TEAM_SHORT", "").strip(),
        db_path=Path(db),
        league=load_league_yaml(),
    )
