"""Glue: SQLite -> baseline projections under this league's scoring."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from hockey.db import get_meta
from hockey.projection.baseline import (
    DEFAULT_GP_CAP,
    DEFAULT_K,
    DEFAULT_WEIGHTS,
    Projection,
    position_means,
    project_player,
)
from hockey.scoring.rules import ScoringRules
from hockey.sources.nhl import SeasonLine


@dataclass
class RosterRow:
    fantrax_id: str
    name: str
    positions: str
    pos_group: str | None
    nhl_team: str | None
    status: str | None
    slot: str | None
    nhl_id: int | None
    match_method: str | None
    projection: Projection | None


class Valuer:
    def __init__(self, conn: sqlite3.Connection, rules: ScoringRules, league: dict):
        self.conn = conn
        self.rules = rules
        cfg = league.get("projection") or {}
        self.weights = tuple(cfg.get("season_weights") or DEFAULT_WEIGHTS)
        self.k = {**DEFAULT_K, **(cfg.get("regression_games") or {})}
        self.gp_cap = {**DEFAULT_GP_CAP, **(cfg.get("gp_cap") or {})}
        seasons = get_meta(conn, "seasons") or {}
        self.seasons: list[int] = seasons.get("completed") or []
        self.lines: dict[int, list[SeasonLine]] = defaultdict(list)
        all_lines = []
        for r in conn.execute("SELECT * FROM nhl_stat_season"):
            ln = SeasonLine(
                r["nhl_id"], r["season"], "", None, r["pos_group"], r["team"], r["gp"], json.loads(r["stats"])
            )
            self.lines[ln.nhl_id].append(ln)
            all_lines.append(ln)
        self.means = position_means(all_lines, self.seasons[0]) if self.seasons else {}

    def project(self, nhl_id: int) -> Projection | None:
        return project_player(
            self.lines.get(nhl_id, []),
            self.seasons,
            self.means,
            self.rules,
            weights=self.weights,
            k=self.k,
            gp_cap=self.gp_cap,
        )

    def team_roster(self, team_id: str) -> list[RosterRow]:
        rows = self.conn.execute(
            """SELECT re.*, fp.name, fp.positions, fp.pos_group, fp.nhl_team, pm.nhl_id, pm.method
               FROM roster_entry re
               JOIN fantrax_player fp ON fp.fantrax_id = re.fantrax_id
               LEFT JOIN player_map pm ON pm.fantrax_id = re.fantrax_id
               WHERE re.team_id = ?""",
            (team_id,),
        ).fetchall()
        return [
            RosterRow(
                fantrax_id=r["fantrax_id"],
                name=r["name"],
                positions=r["positions"] or "",
                pos_group=r["pos_group"],
                nhl_team=r["nhl_team"],
                status=r["status"],
                slot=r["slot"],
                nhl_id=r["nhl_id"],
                match_method=r["method"],
                projection=self.project(r["nhl_id"]) if r["nhl_id"] else None,
            )
            for r in rows
        ]
