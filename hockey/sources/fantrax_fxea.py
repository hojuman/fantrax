"""Fantrax's published, keyless `fxea` API (read-only GETs).

Response shapes are only loosely documented and are verified by `hockey probe` against the real
league. Parsers are deliberately tolerant and raise FantraxShapeError (pointing at `hockey probe`)
rather than returning partial garbage. Fantrax reports errors as HTTP 200 + an error object in the
body, so every response goes through `body_error()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from hockey.http import DAY, HOUR, FetchError, HttpClient, Response
from hockey.sources.teams import normalize_team, pos_group

BASE = "https://www.fantrax.com/fxea/general"


class FantraxError(Exception):
    """Fantrax refused the request (e.g. private league, bad league id)."""


class FantraxShapeError(Exception):
    """The response didn't look like we expected; Fantrax probably changed something."""


def body_error(data: Any) -> str | None:
    """Return the error message if a (HTTP 200) Fantrax body is actually an error, else None."""
    if not isinstance(data, dict):
        return None
    for key in ("error", "errors", "errorMessage", "pageError"):
        if key in data and data[key]:
            err = data[key]
            if isinstance(err, dict):
                return str(err.get("message") or err.get("title") or err.get("code") or err)
            return str(err)
    if data.get("success") is False or data.get("status") in ("ERROR", "error"):
        return str(data.get("message") or data)
    return None


def _cache_ok(resp: Response) -> bool:
    try:
        return body_error(resp.json()) is None
    except ValueError:
        return False


@dataclass
class FantraxPlayer:
    fantrax_id: str
    name: str  # "First Last"
    nhl_team: str | None
    positions: str
    pos_group: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RosterRow:
    team_id: str
    team_name: str | None
    fantrax_id: str
    slot: str | None
    status: str | None


def display_name(name: str) -> str:
    """'Aho, Sebastian' -> 'Sebastian Aho'; leaves 'Sebastian Aho' alone."""
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


class FxeaClient:
    def __init__(self, http: HttpClient):
        self.http = http

    def _get(self, method: str, params: dict[str, Any], ttl: float | None) -> Any:
        resp = self.http.get(f"{BASE}/{method}", params, ttl=ttl, cache_if=_cache_ok)
        if resp.status != 200:
            raise FetchError(resp.url, resp.status, f"Fantrax {method} failed")
        try:
            data = resp.json()
        except ValueError as e:
            raise FantraxShapeError(f"{method}: response is not JSON ({resp.text[:120]!r})") from e
        err = body_error(data)
        if err:
            raise FantraxError(f"{method}: {err}")
        return data

    def player_ids(self) -> Any:
        return self._get("getPlayerIds", {"sport": "NHL"}, ttl=7 * DAY)

    def league_info(self, league_id: str) -> Any:
        return self._get("getLeagueInfo", {"leagueId": league_id}, ttl=DAY)

    def team_rosters(self, league_id: str) -> Any:
        return self._get("getTeamRosters", {"leagueId": league_id}, ttl=HOUR)

    def standings(self, league_id: str) -> Any:
        return self._get("getStandings", {"leagueId": league_id}, ttl=HOUR)


# --------------------------------------------------------------------------- parsers

_ID_KEYS = ("fantraxId", "id", "scorerId", "playerId")


def parse_player_ids(data: Any) -> list[FantraxPlayer]:
    """getPlayerIds: {fantraxId: {name, team, position, ...}} (or a list of such objects)."""
    items: list[tuple[str | None, dict]]
    if isinstance(data, dict):
        items = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    elif isinstance(data, list):
        items = [(None, v) for v in data if isinstance(v, dict)]
    else:
        raise FantraxShapeError("getPlayerIds: expected an object or list")
    out = []
    for key, v in items:
        fid = next((str(v[k]) for k in _ID_KEYS if v.get(k)), key)
        name = v.get("name") or v.get("playerName")
        if not fid or not name:
            continue
        positions = str(v.get("position") or v.get("positions") or v.get("pos") or "")
        # The live feed carries the team as teamShortName/teamName (probe 2026-09-29), not "team".
        team = next(
            (t for k in ("team", "teamShortName", "teamName") if (t := normalize_team(v.get(k)))), None
        )
        known = {
            "name",
            "playerName",
            "team",
            "teamShortName",
            "teamName",
            "shortName",
            "position",
            "positions",
            "pos",
            *_ID_KEYS,
        }
        out.append(
            FantraxPlayer(
                fantrax_id=fid,
                name=display_name(str(name)),
                nhl_team=team,
                positions=positions,
                pos_group=pos_group(positions),
                extra={k: val for k, val in v.items() if k not in known},
            )
        )
    if items and not out:
        raise FantraxShapeError("getPlayerIds: no entries had an id and a name; run `hockey probe`")
    return out


def parse_teams(info: dict) -> dict[str, dict[str, str]]:
    """getLeagueInfo -> {team_id: {name, short_name}}.

    teamInfo has only id + name (verified live); short names come from the matchups list.
    """
    raw = info.get("teamInfo") or info.get("teams") or {}
    if isinstance(raw, list):
        raw = {str(t.get("id")): t for t in raw if isinstance(t, dict) and t.get("id")}
    teams = {}
    for tid, t in raw.items():
        if isinstance(t, dict):
            teams[str(t.get("id") or tid)] = {
                "name": str(t.get("name") or t.get("teamName") or tid),
                "short_name": str(t.get("shortName") or t.get("abbrev") or ""),
            }
    for period in info.get("matchups") or []:
        for m in (period or {}).get("matchupList") or []:
            for side in ("home", "away"):
                t = (m or {}).get(side) or {}
                tid = str(t.get("id") or "")
                if tid in teams and not teams[tid]["short_name"] and t.get("shortName"):
                    teams[tid]["short_name"] = str(t["shortName"])
    return teams


def parse_pool(info: dict) -> dict[str, dict[str, str]]:
    """getLeagueInfo player pool -> {fantrax_id: {status, eligible}} (status: FA / W / team id)."""
    raw = info.get("playerInfo") or {}
    pool = {}
    if isinstance(raw, dict):
        for fid, p in raw.items():
            if isinstance(p, dict):
                pool[str(fid)] = {
                    "status": str(p.get("status") or ""),
                    "eligible": str(p.get("eligiblePos") or p.get("eligiblePositions") or ""),
                }
    return pool


_POINTS_KEYS = ("points", "pointsPerStat", "fpts", "weight", "value")
_CODE_KEYS = ("shortName", "code", "abbrev", "statCode", "name")
_CAT_KEYS = ("scoringCategory", "category", "stat", "statCategory")
_GOALIE_RE = re.compile(r"goal(ie|ies|tend|tending)", re.I)


def _stat_config(d: dict, key: str | None = None) -> tuple[str, float] | None:
    pts = next((d[k] for k in _POINTS_KEYS if isinstance(d.get(k), (int, float))), None)
    if pts is None:
        return None
    for ck in _CAT_KEYS:
        cat = d.get(ck)
        if isinstance(cat, dict):
            code = next((cat[k] for k in _CODE_KEYS if isinstance(cat.get(k), str)), None)
            if code:
                return code, float(pts)
    code = next((d[k] for k in _CODE_KEYS[:4] if isinstance(d.get(k), str)), None) or key
    return (code, float(pts)) if code else None


def _group_hint(key: str | None, d: Any) -> str | None:
    labels = [key or ""]
    if isinstance(d, dict):
        g = d.get("group")
        labels += (
            [str(g.get("name", "")) + " " + str(g.get("code", ""))] if isinstance(g, dict) else [str(g or "")]
        )
        labels.append(str(d.get("groupName") or ""))
    text = " ".join(labels)
    if _GOALIE_RE.search(text):
        return "goalie"
    if re.search(r"skat", text, re.I):
        return "skater"
    return None


def _config_group(group: str, position_code: str | None) -> str:
    """Scoring group for a config: position-specific configs become F / D overrides."""
    pos = (position_code or "DEFAULT").upper()
    if pos in ("DEFAULT", "-1", ""):
        return group
    if pos == "G":
        return "goalie"
    if pos == "D":
        return "D"
    return "F"  # C / LW / RW / F: forward-specific weights


def _group_from_label(label: Any) -> str | None:
    if isinstance(label, dict):
        label = f"{label.get('code', '')} {label.get('name', '')}"
    return _group_hint(str(label or ""), None)


def _scoring_from_settings(ss: dict) -> dict[str, dict[str, float]]:
    """scoringSystem.scoringCategorySettings: [{group: {code: HOCKEY_SKATING, ...}, configs: [
    {points, position: {code: DEFAULT}, scoringCategory: {shortName: "G", ...}}]}] (verified live)."""
    found: dict[str, dict[str, float]] = {}
    for grp in ss.get("scoringCategorySettings") or []:
        if not isinstance(grp, dict):
            continue
        group = _group_from_label(grp.get("group")) or "skater"
        for cfg in grp.get("configs") or []:
            cat = cfg.get("scoringCategory") or {}
            code = cat.get("shortName") or cat.get("code")
            pts = cfg.get("points")
            if not code or not isinstance(pts, (int, float)):
                continue
            target = _config_group(group, (cfg.get("position") or {}).get("code"))
            found.setdefault(target, {})[code] = float(pts)
    return found


_POINTS_STR = re.compile(r"^points(-?\d+(?:\.\d+)?)$")


def _scoring_from_categories(ss: dict) -> dict[str, dict[str, float]]:
    """scoringSystem.scoringCategories: {"SKATING": {"G": {"Default": "points2"}}, "GOALIE": ...}."""
    found: dict[str, dict[str, float]] = {}
    for label, codes in (ss.get("scoringCategories") or {}).items():
        group = _group_from_label(label) or "skater"
        if not isinstance(codes, dict):
            continue
        for code, by_pos in codes.items():
            if not isinstance(by_pos, dict):
                continue
            for pos, val in by_pos.items():
                m = _POINTS_STR.match(str(val).strip())
                if m:
                    target = _config_group(group, "DEFAULT" if pos.lower() == "default" else pos)
                    found.setdefault(target, {})[code] = float(m.group(1))
    return found


def scoring_consistency(info: dict) -> list[str]:
    """Differences between Fantrax's two copies of the scoring weights (should be none)."""
    ss = info.get("scoringSystem")
    if not isinstance(ss, dict):
        return []
    a, b = _scoring_from_settings(ss), _scoring_from_categories(ss)
    if not a or not b:
        return []
    diffs = []
    for group in sorted(set(a) | set(b)):
        for code in sorted(set(a.get(group, {})) | set(b.get(group, {}))):
            x, y = a.get(group, {}).get(code), b.get(group, {}).get(code)
            if x is None or y is None or abs(x - y) > 1e-9:
                diffs.append(f"{group} {code}: settings={x} categories={y}")
    return diffs


def parse_scoring(info: dict) -> dict[str, dict[str, float]] | None:
    """Per-stat point weights from getLeagueInfo.

    Returns {"skater": {code: pts}, "goalie": {code: pts}, optionally "F"/"D": {...}} or None
    (the caller then falls back to data/league.yaml). Tries the verified structure first, then
    the string form, then a heuristic walk in case Fantrax reshapes the response.
    """
    ss = info.get("scoringSystem")
    if isinstance(ss, dict):
        found = _scoring_from_settings(ss) or _scoring_from_categories(ss)
        if found:
            return found
    return _scoring_heuristic(info)


def _scoring_heuristic(info: dict) -> dict[str, dict[str, float]] | None:
    roots = [(k, v) for k, v in info.items() if "scor" in k.lower()]
    found: dict[str, dict[str, float]] = {}

    def walk(key: str | None, node: Any, group: str | None) -> None:
        group = _group_hint(key, node) or group
        if isinstance(node, dict):
            cfg = _stat_config(node, key)
            if cfg:
                code, pts = cfg
                found.setdefault(group or "skater", {})[code] = pts
                return
            for k, v in node.items():
                walk(k, v, group)
        elif isinstance(node, list):
            for v in node:
                walk(None, v, group)

    for k, v in roots:
        walk(k, v, None)
    return found or None


def check_roster_info(info: dict, league: dict) -> list[str]:
    """Compare Fantrax rosterInfo with data/league.yaml; return human-readable mismatches."""
    ri = info.get("rosterInfo") or {}
    roster = (league or {}).get("roster") or {}
    active = roster.get("active") or {}
    out = []

    def cmp(label: str, fantrax: Any, ours: Any) -> None:
        if fantrax is not None and ours is not None and fantrax != ours:
            out.append(f"{label}: Fantrax says {fantrax}, data/league.yaml says {ours}")

    cmp("max players", ri.get("maxTotalPlayers"), roster.get("max_players"))
    cmp("active players", ri.get("maxTotalActivePlayers"), sum(active.values()) if active else None)
    cmp("reserve players", ri.get("maxTotalReservePlayers"), roster.get("reserve"))
    for pos, c in (ri.get("positionConstraints") or {}).items():
        cmp(f"active {pos}", (c or {}).get("maxActive"), active.get(pos))
    return out


def parse_rosters(data: Any) -> list[RosterRow]:
    """getTeamRosters -> one row per rostered player."""
    rosters = data.get("rosters") if isinstance(data, dict) else None
    if rosters is None:
        raise FantraxShapeError("getTeamRosters: no 'rosters' key; run `hockey probe`")
    if isinstance(rosters, list):
        rosters = {str(r.get("teamId") or r.get("id")): r for r in rosters if isinstance(r, dict)}
    rows = []
    for tid, team in rosters.items():
        if not isinstance(team, dict):
            continue
        for item in team.get("rosterItems") or team.get("players") or []:
            fid = next((str(item[k]) for k in _ID_KEYS if item.get(k)), None)
            if not fid:
                continue
            rows.append(
                RosterRow(
                    team_id=str(tid),
                    team_name=team.get("teamName") or team.get("name"),
                    fantrax_id=fid,
                    slot=item.get("position") or item.get("posShortName"),
                    status=item.get("status"),
                )
            )
    return rows
