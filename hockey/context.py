"""Shared setup for anything that builds reports: the CLI commands and the AI layer's tools.

Everything here reads the local database (plus the cached NHL schedule); nothing acts on Fantrax.
Errors are raised as ``ContextError`` with a user-facing message, so the CLI can print them and the
AI tools can hand them back to the model.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from hockey.config import Settings
from hockey.db import get_meta
from hockey.lineup.periods import Period, current_period, next_period, parse_periods
from hockey.sync import find_my_team, load_rules
from hockey.valuation import RosterRow, Valuer


class ContextError(Exception):
    pass


def my_team(conn: sqlite3.Connection, settings: Settings, team: str | None = None) -> sqlite3.Row:
    if team:
        settings.my_team_name, settings.my_team_short = team, team
    row = find_my_team(conn, settings)
    if row is None:
        raise ContextError(f"Team {settings.my_team_name!r} not found. Run `hockey sync` first.")
    return row


def team_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fantasy_team WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)", (name, name)
    ).fetchone()


def choose_period(
    conn: sqlite3.Connection, period: int | None = None, current: bool = False, now: datetime | None = None
) -> Period:
    """The roster period asked for; by default the next lineup lock."""
    periods = parse_periods(get_meta(conn, "roster_periods") or [])
    if not periods:
        raise ContextError(
            "No roster periods stored. Run `hockey sync` (they come from Fantrax getLeagueInfo)."
        )
    now = now or datetime.now(UTC)
    if period is not None:
        chosen = next((p for p in periods if p.number == period), None)
    elif current:
        chosen = current_period(periods, now)
    else:
        chosen = next_period(periods, now)
    if chosen is None:
        raise ContextError(f"No matching roster period (known: {periods[0].number}–{periods[-1].number}).")
    return chosen


def ages_this_season(conn: sqlite3.Connection) -> dict[int, float]:
    """NHL id -> age at the start of this season, from birth dates stored with the NHL rosters."""
    from hockey.keeper.aging import age_on, season_start
    from hockey.sources.nhl import current_season

    start = season_start(current_season(date.today()) // 10000)
    ages = {}
    for r in conn.execute("SELECT nhl_id, birth_date FROM nhl_player WHERE birth_date IS NOT NULL"):
        age = age_on(r["birth_date"], start)
        if age is not None:
            ages[r["nhl_id"]] = age
    return ages


def resolve(names: str, rows: list[RosterRow], where: str) -> list[RosterRow]:
    """Comma-separated names (or Fantrax ids) -> RosterRows from ``rows``; raises on unknown/ambiguous."""
    from hockey.idmap.normalize import basic

    out = []
    for raw in [n.strip() for n in names.split(",") if n.strip() and n.strip() != "-"]:
        by_id = [r for r in rows if r.fantrax_id == raw]
        exact = [r for r in rows if basic(r.name) == basic(raw)]
        partial = [r for r in rows if basic(raw) in basic(r.name)]
        found = by_id or exact or partial
        if not found:
            raise ContextError(f"No player matching {raw!r} {where}.")
        if len(found) > 1:
            listing = "; ".join(f"{r.name} ({r.owner}, {r.fantrax_id})" for r in found[:8])
            raise ContextError(f"{raw!r} is ambiguous {where}: {listing}. Use the Fantrax id.")
        out.append(found[0])
    return out


def league_intel(conn: sqlite3.Connection, settings: Settings, me: sqlite3.Row, valuer: Valuer | None = None):
    """LeagueIntel for my team, with this period's opponent when the schedule is known."""
    from hockey.intel.league import build_intel, opponent_for

    valuer = valuer or Valuer(conn, load_rules(conn), settings.league)
    teams = conn.execute(
        "SELECT DISTINCT ft.team_id, ft.name FROM fantasy_team ft JOIN roster_entry re ON re.team_id = ft.team_id"
    ).fetchall()
    rosters = {t["team_id"]: (t["name"], valuer.team_roster(t["team_id"])) for t in teams}
    periods = parse_periods(get_meta(conn, "roster_periods") or [])
    now = datetime.now(UTC)
    period = next_period(periods, now) or current_period(periods, now)
    opp = opponent_for(get_meta(conn, "matchups") or [], period.number, me["team_id"]) if period else None
    slots = (settings.league.get("roster") or {}).get("active") or None
    return build_intel(rosters, me["team_id"], slots, opponent_id=opp), period


@dataclass
class LeagueContext:
    """Everything a report needs, opened once: settings, db, http (for the NHL schedule), my team."""

    settings: Settings
    conn: sqlite3.Connection
    http: object  # HttpClient (or a test double)
    me: sqlite3.Row
    _valuer: Valuer | None = None
    _games: dict[int, list] = field(default_factory=dict)

    @property
    def league(self) -> dict:
        return self.settings.league

    @property
    def roster_cfg(self) -> dict:
        return self.league.get("roster") or {}

    @property
    def valuer(self) -> Valuer:
        if self._valuer is None:
            self._valuer = Valuer(self.conn, load_rules(self.conn), self.league)
        return self._valuer

    def games(self, period: Period) -> list:
        """Regular-season NHL games in the period (the NHL schedule, cached by the HTTP layer)."""
        from hockey.sources.nhl import NhlClient

        if period.number not in self._games:
            self._games[period.number] = NhlClient(self.http, date.today()).schedule(period.start, period.end)
        return self._games[period.number]

    def lineup(self, period: Period, outs: list[str] | None = None, news_outs: dict[str, str] | None = None):
        from hockey.lineup.report import build_report

        cfg = self.roster_cfg
        return build_report(
            self.valuer,
            self.me["team_id"],
            period,
            self.games(period),
            outs or [],
            slots=cfg.get("active") or None,
            reserve_max=cfg.get("reserve", 6),
            ir_max=cfg.get("injured_reserve", 3),
            news_outs=news_outs,
        )

    def waivers(self, period: Period, **kw):
        from hockey.keepers import load_keepers
        from hockey.waivers.report import build_waivers

        kw.setdefault("keepers", load_keepers())
        return build_waivers(
            self.valuer, self.me["team_id"], period, self.games(period), self.roster_cfg, **kw
        )

    def intel(self):
        return league_intel(self.conn, self.settings, self.me, self.valuer)

    def trade(
        self,
        give: str,
        get: str,
        *,
        give_picks: list[str] | None = None,
        get_picks: list[str] | None = None,
        partner: str | None = None,
        keeper_weight: float = 0.5,
    ):
        """TradeReport for a proposal; names are comma-separated names or Fantrax ids."""
        from hockey.keepers import load_keepers
        from hockey.trade.analyze import Pick, TradeError, analyze

        me = self.me
        pool = self.valuer.league_players()
        mine = self.valuer.team_roster(me["team_id"])
        others = [r for r in pool if r.owner not in (me["name"], "FA", "W")]
        give_rows = resolve(give, mine, "on your roster")
        get_rows = resolve(get, others, "on another team")
        owners = {r.owner for r in get_rows}
        if partner:
            prow = team_by_name(self.conn, partner)
            if prow is None:
                raise ContextError(f"No fantasy team {partner!r}.")
            owners.add(prow["name"])
        if len(owners) != 1:
            raise ContextError(
                "Players you receive must all come from one team"
                if owners
                else "Name the other team (--partner) when you receive only picks."
            )
        their_name = owners.pop()
        their_id = self.conn.execute(
            "SELECT team_id FROM fantasy_team WHERE name=?", (their_name,)
        ).fetchone()[0]
        try:
            return analyze(
                mine,
                self.valuer.team_roster(their_id),
                give_rows,
                get_rows,
                pool,
                my_name=me["name"],
                their_name=their_name,
                roster_cfg=self.roster_cfg,
                teams=int(self.league.get("teams", 10)),
                regular_keepers=int((self.league.get("keepers") or {}).get("regular", 10)),
                give_picks=[Pick.parse(p) for p in give_picks or []],
                get_picks=[Pick.parse(p) for p in get_picks or []],
                keepers=load_keepers(),
                keeper_weight=keeper_weight,
                ages=ages_this_season(self.conn),
            )
        except TradeError as e:
            raise ContextError(str(e)) from e

    def keeper_candidates(self, horizon: int = 3, keepers_file=None):
        """(candidates, rules, keeper line) for my roster; fetches NHL career lines (cached 24 h)."""
        from hockey.keeper.aging import age_on, season_start
        from hockey.keeper.plan import Rules, build_candidate
        from hockey.keepers import load_keepers
        from hockey.sources.nhl import NhlClient, current_season
        from hockey.trade.analyze import at_rank, ranked_next_season

        rules = Rules.from_league(self.league)
        roster = self.valuer.team_roster(self.me["team_id"])
        teams = int(self.league.get("teams", 10))
        keeper_line = at_rank(ranked_next_season(self.valuer.league_players()), teams * rules.regular)
        next_start = season_start(current_season(date.today()) // 10000 + 1)
        nhl = NhlClient(self.http, date.today())
        entries = load_keepers(keepers_file)
        cands = []
        for r in roster:
            if r.nhl_id:
                career = nhl.career(r.nhl_id)
                gp, birth, known = career.gp, career.birth_date, True
            else:
                gp, birth, known = 0, None, False
            cands.append(
                build_candidate(
                    r,
                    age_next=age_on(birth, next_start),
                    career_gp=gp,
                    known_career=known,
                    entry=entries.get(r.fantrax_id),
                    keeper_line=keeper_line,
                    rules=rules,
                    horizon=horizon,
                )
            )
        return cands, rules, keeper_line

    def keeper_plan(self, horizon: int = 3, keepers_file=None):
        """Best keeper set for my roster (fetches each player's NHL career line, cached 24 h)."""
        from hockey.keeper.plan import plan

        return plan(*self.keeper_candidates(horizon, keepers_file))

    def keeper_costs(self, horizon: int = 3) -> dict[str, float | None]:
        """Fantrax id -> keeper value lost if he's dropped (None = unknown: no NHL record)."""
        from hockey.keeper.plan import drop_costs

        cands, rules, _ = self.keeper_candidates(horizon)
        costs: dict[str, float | None] = {c.row.fantrax_id: None for c in cands if c.unknown}
        return costs | drop_costs(cands, rules)
