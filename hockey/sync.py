"""Orchestrates `hockey sync`: Fantrax -> NHL -> ID mapping, all into SQLite."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from hockey.config import DATA_DIR, Settings
from hockey.db import get_meta, set_meta
from hockey.http import HttpClient
from hockey.idmap.match import FxRef, Matcher, NhlRef, load_overrides
from hockey.scoring.rules import ScoringConfigError, ScoringRules, build_rules, rules_from_league_yaml
from hockey.sources import fantrax_csv
from hockey.sources.fantrax_fxea import (
    FantraxError,
    FantraxShapeError,
    FxeaClient,
    check_roster_info,
    parse_player_ids,
    parse_pool,
    parse_rosters,
    parse_scoring,
    parse_teams,
    scoring_consistency,
)
from hockey.sources.moneypuck import MoneyPuckClient, MoneyPuckShapeError
from hockey.sources.nhl import NhlClient, SeasonLine, completed_seasons, current_season, season_label


@dataclass
class SyncReport:
    notes: list[str] = field(default_factory=list)  # things you should know
    problems: list[str] = field(default_factory=list)  # things that need action
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------- Fantrax


def _upsert_fantrax_players(conn: sqlite3.Connection, players, *, keep_extra: bool = False) -> None:
    for p in players:
        extra = None if keep_extra else json.dumps(getattr(p, "extra", {}) or {})
        conn.execute(
            """INSERT INTO fantrax_player(fantrax_id, name, nhl_team, positions, pos_group, extra)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(fantrax_id) DO UPDATE SET name=excluded.name, nhl_team=excluded.nhl_team,
                 positions=excluded.positions, pos_group=excluded.pos_group,
                 extra=COALESCE(excluded.extra, fantrax_player.extra)""",
            (p.fantrax_id, p.name, p.nhl_team, p.positions, p.pos_group, extra),
        )


def _upsert_team(conn: sqlite3.Connection, team_id: str, name: str, short: str | None, source: str) -> None:
    conn.execute(
        """INSERT INTO fantasy_team(team_id, name, short_name, source) VALUES (?,?,?,?)
           ON CONFLICT(team_id) DO UPDATE SET name=excluded.name,
             short_name=COALESCE(NULLIF(excluded.short_name, ''), fantasy_team.short_name), source=excluded.source""",
        (team_id, name, short, source),
    )


def resolve_scoring(
    conn: sqlite3.Connection, settings: Settings, fantrax_raw: dict | None, report: SyncReport
) -> None:
    scoring_cfg = settings.league.get("scoring") or {}
    rules: ScoringRules | None = None
    if fantrax_raw:
        try:
            rules = build_rules(
                fantrax_raw,
                source="fantrax",
                code_aliases=scoring_cfg.get("code_aliases"),
                ignore_codes=scoring_cfg.get("ignore_codes"),
            )
        except ScoringConfigError as e:
            report.problems.append(f"Fantrax scoring settings couldn't be mapped: {e}")
    if rules is None or rules.is_empty():
        try:
            rules = rules_from_league_yaml(settings.league)
        except ScoringConfigError as e:
            report.problems.append(f"data/league.yaml scoring: {e}")
            rules = None
        if rules:
            report.notes.append("Scoring weights from data/league.yaml (Fantrax didn't provide usable ones).")
    if rules is None or rules.is_empty():
        report.problems.append(
            "No scoring weights: fill in scoring.skater / scoring.goalie in data/league.yaml "
            "(Fantrax > League > Settings > Scoring). Projections will show 0 FP until then."
        )
        return
    set_meta(conn, "scoring_rules", rules.to_json())


def sync_fantrax(conn: sqlite3.Connection, http: HttpClient, settings: Settings, report: SyncReport) -> None:
    league_id = settings.require_league_id()
    fx = FxeaClient(http)

    players = parse_player_ids(fx.player_ids())
    _upsert_fantrax_players(conn, players)
    report.counts["fantrax players"] = len(players)

    scoring_raw = None
    try:
        info = fx.league_info(league_id)
    except (FantraxError, FantraxShapeError) as e:
        report.problems.append(
            f"getLeagueInfo refused ({e}). If the league is private, fxea may not serve it: use "
            "`hockey import-csv` for rosters and data/league.yaml for scoring."
        )
    else:
        for tid, t in parse_teams(info).items():
            _upsert_team(conn, tid, t["name"], t["short_name"], "fxea")
        pool = parse_pool(info)
        for fid, p in pool.items():
            conn.execute("UPDATE fantrax_player SET pool_status=? WHERE fantrax_id=?", (p["status"], fid))
            if p["eligible"]:
                conn.execute("UPDATE fantrax_player SET positions=? WHERE fantrax_id=?", (p["eligible"], fid))
        report.counts["player pool"] = len(pool)
        scoring_raw = parse_scoring(info)
        for d in scoring_consistency(info):
            report.problems.append(f"Fantrax's two copies of the scoring weights disagree: {d}")
        for m in check_roster_info(info, settings.league):
            report.notes.append(f"Roster rules differ, {m}. Fantrax enforces its own number.")
        # Weekly lock periods and the H2H schedule, for the lineup optimizer / league intel phases.
        set_meta(conn, "roster_periods", info.get("rosterPeriods") or [])
        set_meta(conn, "matchups", info.get("matchups") or [])
        if not scoring_raw:
            report.notes.append(
                "getLeagueInfo had no recognizable scoring weights; run `hockey probe` to inspect."
            )

    resolve_scoring(conn, settings, scoring_raw, report)

    try:
        rows = parse_rosters(fx.team_rosters(league_id))
    except (FantraxError, FantraxShapeError) as e:
        report.problems.append(f"getTeamRosters refused ({e}); use `hockey import-csv` for rosters.")
    else:
        now = time.time()
        conn.execute("DELETE FROM roster_entry WHERE source='fxea'")
        for r in rows:
            if r.team_name:
                _upsert_team(conn, r.team_id, r.team_name, None, "fxea")
            conn.execute(
                "INSERT OR REPLACE INTO roster_entry(team_id, fantrax_id, slot, status, source, as_of) VALUES (?,?,?,?,?,?)",
                (r.team_id, r.fantrax_id, r.slot, r.status, "fxea", now),
            )
        report.counts["rostered players"] = len(rows)
    conn.commit()


def import_csv(conn: sqlite3.Connection, path: Path, team_name: str | None = None) -> dict[str, int]:
    """Import a Fantrax CSV export. With ``team_name`` every row goes on that team's roster;
    otherwise the Status column decides (team short name / FA / W)."""
    rows = fantrax_csv.read_players_csv(path)
    _upsert_fantrax_players(conn, rows, keep_extra=True)
    now = time.time()
    rostered = 0
    if team_name:
        tid = f"csv:{team_name}"
        _upsert_team(conn, tid, team_name, None, "csv")
        conn.execute("DELETE FROM roster_entry WHERE team_id=?", (tid,))
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO roster_entry VALUES (?,?,?,?,?,?)",
                (tid, r.fantrax_id, None, r.status, "csv", now),
            )
        rostered = len(rows)
    else:
        # A whole-league export supersedes every earlier CSV roster.
        conn.execute("DELETE FROM roster_entry WHERE source='csv'")
        for r in rows:
            if fantrax_csv.is_rostered_status(r.status):
                tid = f"csv:{r.status}"
                _upsert_team(conn, tid, r.status, r.status, "csv")
                conn.execute(
                    "INSERT OR REPLACE INTO roster_entry VALUES (?,?,?,?,?,?)",
                    (tid, r.fantrax_id, None, None, "csv", now),
                )
                rostered += 1
            else:
                conn.execute(
                    "UPDATE fantrax_player SET pool_status=? WHERE fantrax_id=?",
                    ("W" if (r.status or "").upper().startswith("W") else "FA", r.fantrax_id),
                )
    conn.commit()
    return {"rows": len(rows), "rostered": rostered}


# ---------------------------------------------------------------------------- NHL


def sync_nhl(
    conn: sqlite3.Connection, http: HttpClient, today: date, report: SyncReport, n_seasons: int = 3
) -> None:
    nhl = NhlClient(http, today)
    seasons = completed_seasons(today, n_seasons)
    cur = current_season(today)
    lines: list[SeasonLine] = []
    for season in [cur, *seasons]:
        season_lines = nhl.skater_season(season) + nhl.goalie_season(season)
        if season == cur and not season_lines:
            continue  # preseason: nothing yet
        lines.extend(season_lines)
    for ln in lines:
        conn.execute(
            "INSERT OR REPLACE INTO nhl_stat_season(nhl_id, season, pos_group, team, gp, stats) VALUES (?,?,?,?,?,?)",
            (ln.nhl_id, ln.season, ln.pos_group, ln.team, ln.gp, json.dumps(ln.stats)),
        )
    # Player universe: current rosters (current team, birth date), then anyone with recent stats.
    roster, failed = nhl.current_rosters()
    if failed:
        report.notes.append(
            f"NHL rosters unavailable for {', '.join(failed)}; their players keep last season's team."
        )
    seen = set()
    for p in roster:
        seen.add(p.nhl_id)
        conn.execute(
            "INSERT OR REPLACE INTO nhl_player VALUES (?,?,?,?,?,?)",
            (p.nhl_id, p.full_name, p.pos_code, p.pos_group, p.team, p.birth_date),
        )
    for ln in sorted(lines, key=lambda x: x.season):  # latest season wins for team
        if ln.nhl_id in seen:
            continue
        conn.execute(
            """INSERT INTO nhl_player(nhl_id, full_name, pos_code, pos_group, team) VALUES (?,?,?,?,?)
               ON CONFLICT(nhl_id) DO UPDATE SET team=excluded.team""",
            (ln.nhl_id, ln.name, ln.pos_code, ln.pos_group, ln.team),
        )
    set_meta(conn, "seasons", {"current": cur, "completed": seasons})
    set_meta(conn, "nhl_roster_ids", sorted(seen))
    report.counts["NHL season lines"] = len(lines)
    report.counts["NHL roster players"] = len(roster)

    # In season: recent-form windows and team games played (for rest-of-season games).
    conn.execute("DELETE FROM nhl_stat_window")
    conn.execute("DELETE FROM nhl_team_games")
    in_season = any(ln.season == cur for ln in lines)
    if in_season:
        until = today - timedelta(days=1)
        for name, days in (("last30", 30), ("last14", 14)):
            window = nhl.window(cur, today - timedelta(days=days), until)
            for ln in window:
                conn.execute(
                    "INSERT OR REPLACE INTO nhl_stat_window VALUES (?,?,?,?,?,?)",
                    (ln.nhl_id, name, until.isoformat(), ln.pos_group, ln.gp, json.dumps(ln.stats)),
                )
            report.counts[f"NHL {name} lines"] = len(window)
        for team, gp in nhl.team_games_played().items():
            conn.execute("INSERT INTO nhl_team_games VALUES (?,?,?,?)", (team, cur, gp, today.isoformat()))
    else:
        report.notes.append("Preseason: projections come from prior seasons only (no games yet this season).")
    conn.commit()


def sync_moneypuck(conn: sqlite3.Connection, http: HttpClient, today: date, report: SyncReport) -> None:
    """MoneyPuck season summaries (xG, ice time, PP time) for the seasons we project from."""
    mp = MoneyPuckClient(http, today)
    total = 0
    cur = current_season(today)
    for season in [cur, *completed_seasons(today, 3)]:
        try:
            lines = mp.season(season)
        except MoneyPuckShapeError as e:
            if season == cur:  # e.g. an HTML placeholder before the season's file exists
                report.notes.append(f"MoneyPuck has no usable {season_label(season)} file yet; skipped.")
            else:
                report.problems.append(f"{e}. Run `hockey probe` and share the MoneyPuck rows.")
            continue
        for ln in lines:
            conn.execute(
                "INSERT OR REPLACE INTO mp_season VALUES (?,?,?,?,?)",
                (ln.nhl_id, ln.season, ln.pos_group, ln.gp, json.dumps(ln.data)),
            )
        total += len(lines)
    report.counts["MoneyPuck lines"] = total
    conn.commit()


# ---------------------------------------------------------------------------- ID mapping


def relevant_fantrax_ids(conn: sqlite3.Connection) -> set[str]:
    ids = {r[0] for r in conn.execute("SELECT fantrax_id FROM roster_entry")}
    ids |= {
        r[0]
        for r in conn.execute(
            "SELECT fantrax_id FROM fantrax_player WHERE pool_status IS NOT NULL AND pool_status != ''"
        )
    }
    return ids


def run_idmap(conn: sqlite3.Connection, report: SyncReport, overrides_path: Path | None = None) -> None:
    nhl = [
        NhlRef(r["nhl_id"], r["full_name"], r["pos_group"], r["team"])
        for r in conn.execute("SELECT * FROM nhl_player")
    ]
    if not nhl:
        report.problems.append("No NHL players loaded; ID mapping skipped.")
        return
    fx = [
        FxRef(r["fantrax_id"], r["name"], r["pos_group"], r["nhl_team"], json.loads(r["extra"] or "{}"))
        for r in conn.execute("SELECT * FROM fantrax_player")
    ]
    relevant = relevant_fantrax_ids(conn)
    # Only map players that matter when we know who they are; the full getPlayerIds list includes
    # juniors, Europeans and retirees that would just be noise.
    if relevant:
        fx = [f for f in fx if f.fantrax_id in relevant]
    previous = {
        r[0]: r[1] for r in conn.execute("SELECT fantrax_id, nhl_id FROM player_map WHERE method != 'fuzzy'")
    }
    overrides = load_overrides(overrides_path or DATA_DIR / "id_overrides.csv")
    matched, unmatched = Matcher(nhl).match_all(fx, overrides, previous)

    now = time.time()
    by_id = {f.fantrax_id: f for f in fx}
    conn.execute("DELETE FROM player_map")
    conn.execute("DELETE FROM id_unmatched")
    for m in matched:
        conn.execute(
            "INSERT INTO player_map VALUES (?,?,?,?,?)", (m.fantrax_id, m.nhl_id, m.method, m.confidence, now)
        )
    for u in unmatched:
        f = by_id[u.fantrax_id]
        conn.execute(
            "INSERT INTO id_unmatched VALUES (?,?,?,?,?,?,?,?)",
            (
                u.fantrax_id,
                f.name,
                f.team,
                f.pos_group,
                u.reason,
                json.dumps(u.candidates),
                int(u.fantrax_id in relevant),
                now,
            ),
        )
    conn.commit()
    rostered = {
        r[0]: (r[1] or "").upper() for r in conn.execute("SELECT fantrax_id, status FROM roster_entry")
    }
    unmatched_rostered = [u for u in unmatched if u.fantrax_id in rostered]
    report.counts["ID matched"] = len(matched)
    report.counts["unmatched: rostered"] = len(unmatched_rostered)
    report.counts["unmatched: pool, no NHL record"] = len(unmatched) - len(unmatched_rostered)
    fuzzy = sum(1 for m in matched if m.method == "fuzzy")
    if fuzzy:
        report.notes.append(
            f"{fuzzy} fuzzy ID matches: check `hockey ids --fuzzy` and confirm in data/id_overrides.csv."
        )
    if unmatched_rostered:
        names = []
        for u in unmatched_rostered[:10]:
            f = by_id[u.fantrax_id]
            likely_prospect = f.team is None or rostered.get(u.fantrax_id) == "MINORS"
            names.append(f.name + (" (rookie/prospect?)" if likely_prospect else ""))
        more = f" and {len(unmatched_rostered) - 10} more" if len(unmatched_rostered) > 10 else ""
        report.notes.append(
            f"Rostered players without an NHL id: {', '.join(names)}{more}. See `hockey ids --unmatched`."
        )
    elif rostered:
        report.notes.append("All rostered players matched to an NHL id.")
    if len(unmatched) > len(unmatched_rostered):
        report.notes.append(
            f"{len(unmatched) - len(unmatched_rostered)} pool players have no NHL record "
            "(expected: juniors, Europeans, prospects)."
        )


def find_my_team(conn: sqlite3.Connection, settings: Settings) -> sqlite3.Row | None:
    want = {settings.my_team_name.lower(), settings.my_team_short.lower()} - {""}
    rows = conn.execute("SELECT * FROM fantasy_team ORDER BY source='fxea' DESC").fetchall()
    for r in rows:
        if r["name"].lower() in want or (r["short_name"] or "").lower() in want:
            return r
    return None


def load_rules(conn: sqlite3.Connection) -> ScoringRules:
    data = get_meta(conn, "scoring_rules")
    return ScoringRules.from_json(data) if data else ScoringRules(source="none")
