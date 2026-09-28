"""MoneyPuck season summaries: expected goals, ice time and power-play time.

Data: MoneyPuck.com. Terms (read 2026-09-29): free for non-commercial use; "clearly credit
MoneyPuck.com in all cases where you are showing anything using our data as an input". Every view
that uses these numbers must print CREDIT.

One CSV per season and group; rows are split by `situation` (all, 5on5, 5on4, 4on5, other).
`playerId` is the NHL player id, so no mapping is needed. Column names aren't documented; the parser
checks for the ones it needs and fails with the header it actually got.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from hockey.http import DAY, FOREVER, FetchError, HttpClient
from hockey.sources.nhl import current_season

CREDIT = "Expected goals and ice-time data: MoneyPuck.com"
BASE = "https://moneypuck.com/moneypuck/playerData/seasonSummary"

SKATER_COLS = (
    "playerId",
    "situation",
    "games_played",
    "icetime",
    "I_F_xGoals",
    "I_F_goals",
    "I_F_shotsOnGoal",
)
GOALIE_COLS = ("playerId", "situation", "games_played", "icetime", "xGoals", "goals", "ongoal")


class MoneyPuckShapeError(Exception):
    pass


@dataclass
class MpLine:
    nhl_id: int
    season: int
    pos_group: str
    gp: int
    data: dict[str, float] = field(default_factory=dict)


def _f(v: str | None) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _rows(text: str, required: tuple[str, ...], what: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [c for c in required if c not in header]
    if missing:
        raise MoneyPuckShapeError(f"MoneyPuck {what}: missing columns {missing}; header was {header[:40]}")
    return list(reader)


def parse_skaters(text: str, season: int) -> list[MpLine]:
    by_id: dict[int, dict[str, dict]] = {}
    for r in _rows(text, SKATER_COLS, "skaters.csv"):
        if r["situation"] in ("all", "5on4"):
            by_id.setdefault(int(r["playerId"]), {})[r["situation"]] = r
    out = []
    for pid, sit in by_id.items():
        a = sit.get("all")
        if a is None:
            continue
        pp = sit.get("5on4") or {}
        pos = (a.get("position") or "").upper()
        out.append(
            MpLine(
                pid,
                season,
                "D" if pos == "D" else "F",
                int(_f(a["games_played"])),
                {
                    "toi_min": _f(a["icetime"]) / 60.0,
                    "pp_toi_min": _f(pp.get("icetime")) / 60.0,
                    "ixg": _f(a["I_F_xGoals"]),
                    "g": _f(a["I_F_goals"]),
                    "sog": _f(a["I_F_shotsOnGoal"]),
                },
            )
        )
    return out


def parse_goalies(text: str, season: int) -> list[MpLine]:
    out = []
    for r in _rows(text, GOALIE_COLS, "goalies.csv"):
        if r["situation"] != "all":
            continue
        out.append(
            MpLine(
                int(r["playerId"]),
                season,
                "G",
                int(_f(r["games_played"])),
                {
                    "toi_min": _f(r["icetime"]) / 60.0,
                    "xga": _f(r["xGoals"]),
                    "ga": _f(r["goals"]),
                    "sa": _f(r["ongoal"]),
                },
            )
        )
    return out


class MoneyPuckClient:
    def __init__(self, http: HttpClient, today: date):
        self.http = http
        self.today = today

    def _csv(self, season: int, group: str) -> str | None:
        ttl = FOREVER if season < current_season(self.today) else DAY
        resp = self.http.get(f"{BASE}/{season // 10000}/regular/{group}.csv", ttl=ttl)
        if resp.status == 404:
            return None  # e.g. the current season before opening night
        if resp.status != 200:
            raise FetchError(resp.url, resp.status, f"MoneyPuck {group}.csv failed")
        return resp.text

    def season(self, season: int) -> list[MpLine]:
        sk, go = self._csv(season, "skaters"), self._csv(season, "goalies")
        return (parse_skaters(sk, season) if sk else []) + (parse_goalies(go, season) if go else [])
