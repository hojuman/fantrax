"""NHL data: api.nhle.com/stats/rest (bulk season stats) and api-web.nhle.com (rosters, schedule).

Both are unofficial/undocumented but public. We use bulk endpoints (a handful of requests per
season) rather than per-player calls, and cache completed seasons forever.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from hockey.http import DAY, FOREVER, HOUR, FetchError, HttpClient
from hockey.sources.teams import NHL_TEAMS, normalize_team, pos_group

log = logging.getLogger(__name__)

STATS = "https://api.nhle.com/stats/rest/en"
WEB = "https://api-web.nhle.com/v1"
PAGE = 100
REGULAR_SEASON = 2


def season_id(start_year: int) -> int:
    return start_year * 10000 + start_year + 1


def current_season(today: date) -> int:
    """Seasons roll over in September (training camp)."""
    return season_id(today.year if today.month >= 9 else today.year - 1)


def completed_seasons(today: date, n: int = 3) -> list[int]:
    """The n most recent completed regular seasons, most recent first."""
    start = current_season(today) // 10000
    return [season_id(start - i) for i in range(1, n + 1)]


@dataclass
class NhlPlayer:
    nhl_id: int
    full_name: str
    pos_code: str | None
    pos_group: str | None
    team: str | None
    birth_date: str | None = None


@dataclass
class SeasonLine:
    nhl_id: int
    season: int
    name: str
    pos_code: str | None
    pos_group: str
    team: str | None
    gp: int
    stats: dict[str, float] = field(default_factory=dict)


def _n(row: dict, key: str) -> float:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else 0.0


def _last_team(abbrevs: str | None) -> str | None:
    if not abbrevs:
        return None
    return normalize_team(abbrevs.split(",")[-1])


def merge_skater_rows(
    summary: list[dict], realtime: list[dict], faceoffs: list[dict], season: int
) -> list[SeasonLine]:
    rt = {r["playerId"]: r for r in realtime}
    fo = {r["playerId"]: r for r in faceoffs}
    missing = 0
    lines = []
    for s in summary:
        pid = s["playerId"]
        gp = int(_n(s, "gamesPlayed"))
        r, f = rt.get(pid), fo.get(pid)
        if r is None or f is None:
            missing += 1
        r, f = r or {}, f or {}
        g, a = _n(s, "goals"), _n(s, "assists")
        ppg, ppp = _n(s, "ppGoals"), _n(s, "ppPoints")
        shg, shp = _n(s, "shGoals"), _n(s, "shPoints")
        stats = {
            "gp": gp,
            "g": g,
            "a": a,
            "pts": _n(s, "points"),
            "pm": _n(s, "plusMinus"),
            "pim": _n(s, "penaltyMinutes"),
            "ppg": ppg,
            "ppa": ppp - ppg,
            "ppp": ppp,
            "shg": shg,
            "sha": shp - shg,
            "shp": shp,
            "gwg": _n(s, "gameWinningGoals"),
            "otg": _n(s, "otGoals"),
            "sog": _n(s, "shots"),
            "evg": _n(s, "evGoals"),
            "evp": _n(s, "evPoints"),
            "toi_min": _n(s, "timeOnIcePerGame") * gp / 60.0,
            "hit": _n(r, "hits"),
            "blk": _n(r, "blockedShots"),
            "tk": _n(r, "takeaways"),
            "gv": _n(r, "giveaways"),
            "fow": _n(f, "totalFaceoffWins"),
            "fol": _n(f, "totalFaceoffLosses"),
        }
        pos = s.get("positionCode")
        lines.append(
            SeasonLine(
                pid,
                season,
                s.get("skaterFullName", ""),
                pos,
                pos_group(pos) or "F",
                _last_team(s.get("teamAbbrevs")),
                gp,
                stats,
            )
        )
    if missing:
        log.warning("season %s: %d skaters missing realtime/faceoff rows (counted as 0)", season, missing)
    return lines


def goalie_lines(summary: list[dict], season: int) -> list[SeasonLine]:
    lines = []
    for s in summary:
        gp = int(_n(s, "gamesPlayed"))
        stats = {
            "gp": gp,
            "gs": _n(s, "gamesStarted"),
            "w": _n(s, "wins"),
            "l": _n(s, "losses"),
            "otl": _n(s, "otLosses"),
            "ga": _n(s, "goalsAgainst"),
            "sv": _n(s, "saves"),
            "sa": _n(s, "shotsAgainst"),
            "so": _n(s, "shutouts"),
            "toi_min": _n(s, "timeOnIce") / 60.0,
            "g": _n(s, "goals"),
            "a": _n(s, "assists"),
            "pts": _n(s, "points"),
            "pim": _n(s, "penaltyMinutes"),
        }
        lines.append(
            SeasonLine(
                s["playerId"],
                season,
                s.get("goalieFullName", ""),
                "G",
                "G",
                _last_team(s.get("teamAbbrevs")),
                gp,
                stats,
            )
        )
    return lines


def roster_players(data: dict, team: str) -> list[NhlPlayer]:
    out = []
    for group in ("forwards", "defensemen", "goalies"):
        for p in data.get(group) or []:
            first = (p.get("firstName") or {}).get("default", "")
            last = (p.get("lastName") or {}).get("default", "")
            pos = p.get("positionCode")
            out.append(
                NhlPlayer(
                    int(p["id"]), f"{first} {last}".strip(), pos, pos_group(pos), team, p.get("birthDate")
                )
            )
    return out


class NhlClient:
    def __init__(self, http: HttpClient, today: date):
        self.http = http
        self.today = today

    def _ttl(self, season: int) -> float | None:
        return FOREVER if season < current_season(self.today) else 6 * HOUR

    def report(self, kind: str, report: str, season: int) -> list[dict]:
        """All rows of a stats/rest report for one regular season, paginated."""
        rows: list[dict] = []
        start = 0
        while True:
            params = {
                "isAggregate": "false",
                "isGame": "false",
                "sort": json.dumps([{"property": "playerId", "direction": "ASC"}]),
                "start": start,
                "limit": PAGE,
                "cayenneExp": f"gameTypeId={REGULAR_SEASON} and seasonId={season}",
            }
            resp = self.http.get(f"{STATS}/{kind}/{report}", params, ttl=self._ttl(season))
            if resp.status != 200:
                raise FetchError(resp.url, resp.status, f"NHL {kind}/{report} failed")
            body = resp.json()
            page = body.get("data") or []
            rows.extend(page)
            start += len(page)
            if not page or start >= int(body.get("total", 0)):
                return rows

    def skater_season(self, season: int) -> list[SeasonLine]:
        return merge_skater_rows(
            self.report("skater", "summary", season),
            self.report("skater", "realtime", season),
            self.report("skater", "faceoffwins", season),
            season,
        )

    def goalie_season(self, season: int) -> list[SeasonLine]:
        return goalie_lines(self.report("goalie", "summary", season), season)

    def current_rosters(self) -> tuple[list[NhlPlayer], list[str]]:
        """Rosters for the current season, plus the teams that failed (so one bad team can't abort).

        Uses /roster/{team}/{season} directly: /roster/{team}/current is a 307 to the same thing.
        """
        season = current_season(self.today)
        players: list[NhlPlayer] = []
        failed: list[str] = []
        for team in NHL_TEAMS:
            try:
                resp = self.http.get(f"{WEB}/roster/{team}/{season}", ttl=DAY)
            except FetchError as e:
                log.warning("NHL roster %s: %s", team, e)
                failed.append(team)
                continue
            if resp.status != 200:
                log.warning("NHL roster %s: HTTP %s", team, resp.status)
                failed.append(team)
                continue
            players.extend(roster_players(resp.json(), team))
        return players, failed


def season_label(season: int) -> str:
    return f"{season // 10000}-{str(season % 10000)[2:]}"


def as_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)
