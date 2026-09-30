"""Glue: SQLite -> projections under this league's scoring (Phase 2 model; see projection/inseason.py)."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace

from hockey.db import get_meta
from hockey.projection.baseline import (
    DEFAULT_GP_CAP,
    DEFAULT_K,
    DEFAULT_WEIGHTS,
    MEAN_MIN_GP,
    position_means,
    project_player,
)
from hockey.projection.inseason import (
    ON_ROSTER_SHARE,
    ROOKIE_PERCENTILE,
    SEASON_GAMES,
    SMALL_SAMPLE_GP,
    ProjectionV2,
    Window,
    blend,
    percentile,
    rookie_rates,
    xg_adjusted_goals,
)
from hockey.scoring.engine import score
from hockey.scoring.rules import ScoringRules
from hockey.sources.nhl import SeasonLine, season_label


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
    projection: ProjectionV2 | None
    owner: str | None = None  # fantasy team name, or FA / W


class Valuer:
    def __init__(self, conn: sqlite3.Connection, rules: ScoringRules, league: dict):
        self.conn = conn
        self.rules = rules
        cfg = league.get("projection") or {}
        self.weights = tuple(cfg.get("season_weights") or DEFAULT_WEIGHTS)
        self.k = {**DEFAULT_K, **(cfg.get("regression_games") or {})}
        self.gp_cap = {**DEFAULT_GP_CAP, **(cfg.get("gp_cap") or {})}
        seasons = get_meta(conn, "seasons") or {}
        self.current: int | None = seasons.get("current")
        self.seasons: list[int] = seasons.get("completed") or []
        self.roster_ids = set(get_meta(conn, "nhl_roster_ids") or [])

        self.mp: dict[tuple[int, int], dict] = {
            (r["nhl_id"], r["season"]): {**json.loads(r["data"]), "gp": r["gp"]}
            for r in conn.execute("SELECT * FROM mp_season")
        }
        self.raw_goals: dict[tuple[int, int], float] = {}  # before the xG adjustment
        self.lines: dict[int, list[SeasonLine]] = defaultdict(list)  # completed seasons, xG-adjusted
        self.current_lines: dict[int, SeasonLine] = {}
        for r in conn.execute("SELECT * FROM nhl_stat_season"):
            ln = SeasonLine(
                r["nhl_id"], r["season"], "", None, r["pos_group"], r["team"], r["gp"], json.loads(r["stats"])
            )
            if ln.season == self.current:
                self.current_lines[ln.nhl_id] = ln
            else:
                self.raw_goals[(ln.nhl_id, ln.season)] = ln.stats.get("g", 0.0)
                self.lines[ln.nhl_id].append(self._xg_adjust(ln))
        self.windows: dict[tuple[int, str], Window] = {
            (r["nhl_id"], r["window"]): Window(r["gp"], json.loads(r["stats"]))
            for r in conn.execute("SELECT * FROM nhl_stat_window")
        }
        self.team_gp = {r["team"]: r["gp"] for r in conn.execute("SELECT team, gp FROM nhl_team_games")}
        self.nhl_team = {
            r["nhl_id"]: (r["team"], r["pos_group"])
            for r in conn.execute("SELECT nhl_id, team, pos_group FROM nhl_player")
        }

        latest = [
            ln for ls in self.lines.values() for ln in ls if self.seasons and ln.season == self.seasons[0]
        ]
        self.means = position_means(latest, self.seasons[0]) if self.seasons else {}
        self.rookie_fp: dict[str, float] = {}
        for pos in ("F", "D", "G"):
            fps = [
                score(
                    rules, pos, {**{k: v / ln.gp for k, v in ln.stats.items()}, "gp": 1.0}, strict=False
                ).total
                for ln in latest
                if ln.pos_group == pos and ln.gp >= MEAN_MIN_GP
            ]
            self.rookie_fp[pos] = percentile(fps, ROOKIE_PERCENTILE)

    # ------------------------------------------------------------------ building blocks

    def _xg_adjust(self, ln: SeasonLine) -> SeasonLine:
        mp = self.mp.get((ln.nhl_id, ln.season))
        if ln.pos_group == "G" or not mp or "ixg" not in mp:
            return ln
        stats = dict(ln.stats)
        g = stats.get("g", 0.0)
        adj = xg_adjusted_goals(g, mp["ixg"])
        stats["g"], stats["pts"] = adj, stats.get("pts", 0.0) + adj - g
        return replace(ln, stats=stats)

    def _rookie_prior(self, pos: str) -> dict[str, float]:
        mean = self.means.get(pos, {})
        mean_fp = score(self.rules, pos, {**mean, "gp": 1.0}, strict=False).total
        return rookie_rates(mean, mean_fp, self.rookie_fp.get(pos, 0.0))

    def _small_sample(
        self, prior, pos: str, share: float, on_roster: bool, notes: list[str]
    ) -> tuple[dict[str, float], float]:
        """Few NHL games: games from his roster role, rates regressed toward the rookie baseline.

        See SMALL_SAMPLE_GP in projection/inseason.py. Returns (prior rates, prior share).
        """
        w = prior.sample_gp / SMALL_SAMPLE_GP
        role = ON_ROSTER_SHARE.get(pos, 0.7) if on_roster else share
        new_share = w * share + (1 - w) * max(role, share)
        mean, rookie = self.means.get(pos, {}), self._rookie_prior(pos)
        pull = prior.shrink * (1 - w)  # the part of each rate that came from the mean, moved toward rookie
        rates = {
            k: max(0.0, v + pull * (rookie.get(k, mean.get(k, 0.0)) - mean.get(k, 0.0))) if k != "gp" else v
            for k, v in prior.rates.items()
        }
        where = "on an NHL roster, so games from that role" if on_roster else "not on an NHL roster"
        notes.append(
            f"small NHL sample ({prior.sample_gp} GP): {where}; rates pulled toward a rookie baseline"
        )
        return rates, min(1.0, new_share)

    def _prior_notes(self, nhl_id: int) -> list[str]:
        if not self.seasons:
            return []
        last = self.seasons[0]
        mp, g = self.mp.get((nhl_id, last)), self.raw_goals.get((nhl_id, last))
        if not mp or "ixg" not in mp or g is None or abs(g - mp["ixg"]) < 3:
            return []
        tag = "finished above expected" if g > mp["ixg"] else "finished below expected"
        return [f"{season_label(last)}: {g:.0f} G on {mp['ixg']:.1f} ixG ({tag}; prior uses a blend)"]

    def _pp_toi_per_gp(self, nhl_id: int, season: int | None) -> float | None:
        mp = self.mp.get((nhl_id, season)) if season else None
        if not mp or not mp.get("gp") or "pp_toi_min" not in mp:
            return None
        return mp["pp_toi_min"] / mp["gp"]

    # ------------------------------------------------------------------ public

    def project(self, nhl_id: int | None, pos_hint: str | None = None) -> ProjectionV2 | None:
        team, pos = self.nhl_team.get(nhl_id, (None, None)) if nhl_id else (None, None)
        prior = (
            project_player(
                self.lines.get(nhl_id, []),
                self.seasons,
                self.means,
                self.rules,
                weights=self.weights,
                k=self.k,
                gp_cap=self.gp_cap,
            )
            if nhl_id
            else None
        )
        cur = self.current_lines.get(nhl_id) if nhl_id else None
        pos = (prior.pos_group if prior else None) or (cur.pos_group if cur else None) or pos or pos_hint
        if pos is None:
            return None
        season = Window(cur.gp, cur.stats) if cur and cur.gp else None
        in_season = bool(self.team_gp)
        team_gp = self.team_gp.get(team, 0) if in_season else 0
        mp_cur = self.mp.get((nhl_id, self.current)) if nhl_id else None

        on_roster = nhl_id in self.roster_ids
        if prior:
            prior_rates, method = prior.rates, "blend" if season else "prior only"
            prior_share = min(1.0, prior.proj_gp / SEASON_GAMES)
            notes = self._prior_notes(nhl_id)
            if prior.sample_gp < SMALL_SAMPLE_GP:
                prior_rates, prior_share = self._small_sample(prior, pos, prior_share, on_roster, notes)
        else:
            prior_rates = self._rookie_prior(pos)
            prior_share = ON_ROSTER_SHARE.get(pos, 0.7) if on_roster else 0.0
            method = "blend (rookie prior)" if season else "rookie default"
            notes = (
                []
                if season
                else [
                    f"no NHL games: conservative {int(ROOKIE_PERCENTILE * 100)}th-percentile {pos} rate"
                    + ("" if on_roster else "; not on an NHL roster, so 0 games projected")
                ]
            )
        uses_mp = bool(mp_cur) or any((nhl_id, s) in self.mp for s in self.seasons[: len(self.weights)])
        return blend(
            self.rules,
            pos,
            prior=prior,
            prior_rates=prior_rates,
            season=season,
            last30=self.windows.get((nhl_id, "last30")),
            last14=self.windows.get((nhl_id, "last14")),
            season_ixg=(mp_cur or {}).get("ixg"),
            pp_toi_prior=self._pp_toi_per_gp(nhl_id, self.seasons[0] if self.seasons else None),
            pp_toi_season=self._pp_toi_per_gp(nhl_id, self.current),
            team_gp=team_gp,
            prior_share=prior_share,
            method=method,
            notes=notes,
            uses_moneypuck=uses_mp,
        )

    def project_unknown(self, pos_group: str | None) -> ProjectionV2 | None:
        """A Fantrax player with no NHL record: rookie default rate, no games projected."""
        if pos_group is None:
            return None
        return blend(
            self.rules,
            pos_group,
            prior=None,
            prior_rates=self._rookie_prior(pos_group),
            prior_share=0.0,
            method="rookie default",
            notes=["no NHL record: conservative estimate, 0 games projected"],
        )

    def _rows(self, where: str, params: tuple) -> list[RosterRow]:
        rows = self.conn.execute(
            f"""SELECT fp.fantrax_id, fp.name, fp.positions, fp.pos_group, fp.nhl_team, fp.pool_status,
                       re.status, re.slot, pm.nhl_id, pm.method, ft.name AS owner
                FROM fantrax_player fp
                LEFT JOIN roster_entry re ON re.fantrax_id = fp.fantrax_id
                LEFT JOIN fantasy_team ft ON ft.team_id = re.team_id
                LEFT JOIN player_map pm ON pm.fantrax_id = fp.fantrax_id
                WHERE {where}""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            proj = (
                self.project(r["nhl_id"], r["pos_group"])
                if r["nhl_id"]
                else self.project_unknown(r["pos_group"])
            )
            owner = r["owner"] or ("W" if (r["pool_status"] or "").upper().startswith("W") else "FA")
            out.append(
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
                    projection=proj,
                    owner=owner,
                )
            )
        return out

    def team_roster(self, team_id: str) -> list[RosterRow]:
        return self._rows("re.team_id = ?", (team_id,))

    def league_players(self) -> list[RosterRow]:
        """Everyone rostered or in the player pool."""
        return self._rows(
            "re.team_id IS NOT NULL OR (fp.pool_status IS NOT NULL AND fp.pool_status != '')", ()
        )

    def find(self, name: str) -> list[RosterRow]:
        from hockey.idmap.normalize import basic

        by_id = self._rows("fp.fantrax_id = ?", (name.strip(),))
        if by_id:
            return by_id
        want = basic(name)
        rows = self._rows("1=1", ())
        exact = [r for r in rows if basic(r.name) == want]
        return exact or [r for r in rows if want in basic(r.name)]
