"""Read-only tools the model can call. Each wraps a report the CLI already builds and returns compact JSON.

There is deliberately no tool that writes anything: not to Fantrax (the HTTP guard has no way to), not
to the database. `READ_ONLY_TOOLS` is pinned by a test.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hockey.context import ContextError, LeagueContext, choose_period, team_by_name
from hockey.http import FetchError
from hockey.lineup.availability import team_games
from hockey.scoring.engine import score
from hockey.valuation import RosterRow

MAX_ROWS = 40


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(float(x), nd)


def brief(r: RosterRow) -> dict:
    p = r.projection
    out = {
        "name": r.name,
        "fx_id": r.fantrax_id,
        "pos": r.positions,
        "nhl_team": r.nhl_team,
        "owner": r.owner,
        "status": r.status,
    }
    if p:
        out |= {
            "fp_per_gp": _r(p.fp_per_gp, 2),
            "ros_gp": _r(p.ros_gp),
            "ros_fp": _r(p.ros_fp),
            "games_share": _r(p.games_share, 2),
        }
    else:
        out["projection"] = "none (no NHL record)"
    return {k: v for k, v in out.items() if v not in (None, "")}


def _mp(rows: list[RosterRow]) -> bool:
    return any(r.projection and r.projection.uses_moneypuck for r in rows)


# --- tool implementations -------------------------------------------------------------------------


def get_roster(ctx: LeagueContext, team: str | None = None) -> dict:
    row = ctx.me
    if team:
        row = team_by_name(ctx.conn, team)
        if row is None:
            names = [r["name"] for r in ctx.conn.execute("SELECT name FROM fantasy_team ORDER BY name")]
            raise ContextError(f"No fantasy team {team!r}. Teams: {', '.join(names)}")
    rows = ctx.valuer.team_roster(row["team_id"])
    return {"team": row["name"], "players": [brief(r) for r in rows], "_uses_moneypuck": _mp(rows)}


def get_player(ctx: LeagueContext, name: str) -> dict:
    matches = ctx.valuer.find(name)
    if not matches:
        raise ContextError(f"No player matching {name!r}; try part of the last name.")
    if len(matches) > 1:
        return {"ambiguous": [brief(r) for r in matches[:10]], "hint": "call again with the fx_id"}
    r = matches[0]
    out = brief(r)
    p = r.projection
    if p:
        rules = ctx.valuer.rules

        def window(w):
            if not w or not w.gp:
                return None
            return {
                "gp": w.gp,
                "fp_per_gp": _r(
                    score(rules, p.pos_group, {k: v / w.gp for k, v in w.stats.items()}, strict=False).total,
                    2,
                ),
            }

        out |= {
            "method": p.method,
            "fp_per_gp_by_stat": {k: _r(v, 2) for k, v in p.breakdown.top(6)},
            "this_season": window(p.season),
            "last_30_days": window(p.last30),
            "last_14_days": window(p.last14),
            "prior_sample_gp": p.prior.sample_gp if p.prior else 0,
            "notes": p.notes,
            "_uses_moneypuck": p.uses_moneypuck,
        }
    return out


def rank_players(
    ctx: LeagueContext,
    pos: str | None = None,
    available_only: bool = False,
    sort: str = "ros",
    limit: int = 15,
) -> dict:
    rows = [r for r in ctx.valuer.league_players() if r.projection]
    if pos:
        want = pos.upper()
        rows = [
            r
            for r in rows
            if (
                r.pos_group == "F"
                if want == "F"
                else want in {x.strip().upper() for x in r.positions.split(",")}
            )
        ]
    if available_only:
        rows = [r for r in rows if r.owner in ("FA", "W")]
    key = (lambda r: r.projection.ros_fp) if sort == "ros" else (lambda r: r.projection.fp_per_gp)
    rows = sorted(rows, key=key, reverse=True)[: min(limit, MAX_ROWS)]
    return {
        "sorted_by": "ros_fp" if sort == "ros" else "fp_per_gp",
        "players": [brief(r) for r in rows],
        "_uses_moneypuck": _mp(rows),
    }


def best_lineup(ctx: LeagueContext, period: int | None = None, out: list[str] | None = None) -> dict:
    r = ctx.lineup(choose_period(ctx.conn, period), out or [])

    def pw(w):
        d = {"name": w.row.name, "pos": w.row.positions, "nhl_team": w.row.nhl_team, "games": w.avail.games}
        d |= {"expected_games": _r(w.avail.expected), "exp_fp": _r(w.value), "status": w.row.status}
        if w.slot:
            d["slot"] = w.slot
        if w.avail.b2b:
            d["b2b"] = w.avail.b2b
        if w.avail.out or w.avail.flags:
            d["flags"] = (["OUT"] if w.avail.out else []) + w.avail.flags
        return d

    return {
        "period": r.period.number,
        "lock": r.period.start.isoformat(),
        "nhl_games_in_period": r.games_in_period,
        "starters": [pw(w) for w in r.starters],
        "bench": [pw(w) for w in r.bench],
        "ir_and_minors": [pw(w) for w in r.others],
        "empty_slots": {k: v for k, v in r.empty.items() if v},
        "recommended_fp": _r(r.total),
        "current_lineup_fp": _r(r.current_total),
        "move_to_active": [w.row.name for w in r.moves_in],
        "move_to_reserve": [w.row.name for w in r.moves_out],
        "flags": r.flags,
        "_uses_moneypuck": r.uses_moneypuck,
    }


def waiver_options(
    ctx: LeagueContext, pos: str | None = None, protect: list[str] | None = None, period: int | None = None
) -> dict:
    w = ctx.waivers(
        choose_period(ctx.conn, period), protect=protect or [], pos=pos.upper() if pos else None, limit=8
    )

    def opt(o):
        d = {"pickup": brief(o.pickup.row), "drop": o.drop.row.name if o.drop else None, "gain": _r(o.gain)}
        if o.pickup.row.owner == "W":
            d["on_waivers"] = True  # a claim, not an instant add
        return d

    return {
        "period": w.period.number,
        "open_roster_spots": w.open_spots,
        "pickups_ros_gain": [opt(o) for o in w.pickups],
        "streamers_week_gain": [
            opt(o) | {"games": o.pickup.avail.games, "ros_change": _r(o.ros_change)} for o in w.streamers
        ],
        "by_position": [
            {
                "pos": p.pos,
                "your_weakest_starter": p.weakest.row.name if p.weakest else "(empty)",
                "weakest_ros": _r(p.weakest.ros) if p.weakest else 0,
                "best_available": opt(p.best) if p.best else None,
            }
            for p in w.by_position
        ],
        "depth_for_open_spot": [brief(p.row) for p in w.depth[:5]],
        "notes": w.notes,
        "_uses_moneypuck": w.uses_moneypuck,
    }


def evaluate_trade(
    ctx: LeagueContext,
    give: str,
    get: str,
    give_picks: list[str] | None = None,
    get_picks: list[str] | None = None,
    partner: str | None = None,
) -> dict:
    t = ctx.trade(give or "-", get or "-", give_picks=give_picks, get_picks=get_picks, partner=partner)

    def piece(p):
        d = {"name": p.row.name, "ros_fp": _r(p.ros), "next_season_fp": _r(p.next_season)}
        d |= {"value_over_replacement": _r(p.vor), "keeper_value": _r(p.keeper_value)}
        return d | ({"notes": p.notes} if p.notes else {})

    def side(s):
        return {
            "team": s.team,
            "sends": [piece(p) for p in s.sends] + [str(p) for p in s.picks_sent],
            "receives": [piece(p) for p in s.receives] + [str(p) for p in s.picks_received],
            "lineup_change_ros_fp": _r(s.lineup_change),
            "keeper_change": _r(s.keeper_change),
            "pick_change": _r(s.pick_change),
            "must_drop": s.roster_overflow,
            "score": _r(s.score),
        }

    return {
        "verdict": t.verdict,
        "you": side(t.me),
        "them": side(t.them),
        "keeper_weight": t.keeper_weight,
        "notes": t.notes,
        "_uses_moneypuck": t.uses_moneypuck,
    }


def keeper_plan(ctx: LeagueContext, horizon: int = 3) -> dict:
    p = ctx.keeper_plan(horizon)
    return {
        "keeper_line_next_season_fp": _r(p.keeper_line),
        "total_value": _r(p.total),
        "players": [
            {
                "name": c.row.name,
                "choice": c.choice,
                "value": _r(c.value),
                "age_next_season": _r(c.age),
                "career_gp": c.career_gp,
                "times_kept": c.entry.times_kept,
                "franchise_tag": c.entry.franchise_tag,
                "next_seasons_fp": [_r(v) for v in c.yearly],
                **({"notes": c.notes} if c.notes else {}),
            }
            for c in sorted(p.candidates, key=lambda c: -c.value)
        ],
        "warnings": p.warnings,
        "_uses_moneypuck": p.uses_moneypuck,
    }


def league_intel(ctx: LeagueContext) -> dict:
    li, period = ctx.intel()

    def prof(t):
        return {
            "team": t.name,
            "rank": t.overall_rank,
            "lineup_ros_fp": _r(t.lineup, 0),
            "slot_ranks": t.ranks,
            "strengths": t.strengths,
            "weaknesses": t.weaknesses,
        }

    return {
        "period": period.number if period else None,
        "you": prof(li.me) | {"bench": [a.name for a in li.me.bench[:6]]},
        "this_weeks_opponent": prof(li.opponent) if li.opponent else None,
        "teams": [prof(t) for t in li.teams],
        "trade_ideas": [
            {
                "team": t.name,
                "give": s.give.name,
                "get": s.get.name,
                "you_gain": _r(s.my_gain),
                "they_gain": _r(s.their_gain),
            }
            for t, swaps in li.partners
            for s in swaps
        ][:12],
        "_uses_moneypuck": li.uses_moneypuck,
    }


def schedule(ctx: LeagueContext, period: int | None = None) -> dict:
    per = choose_period(ctx.conn, period)
    games = ctx.games(per)
    teams = sorted({t for g in games for t in g.teams()})
    counts = {t: len(team_games(games, t)) for t in teams}
    return {
        "period": per.number,
        "start": per.start.isoformat(),
        "end": per.end.isoformat(),
        "games": len(games),
        "games_per_nhl_team": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


# --- registry -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[..., dict]

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_NAMES = {"type": "array", "items": {"type": "string"}}
_PERIOD = {"type": "integer", "description": "Roster period number; omit for the next weekly lock."}
_POS = {"type": "string", "enum": ["C", "LW", "RW", "D", "G", "F"]}

TOOLS = (
    Tool(
        "get_roster",
        "A fantasy roster with each player's projection: FP per game, projected rest-of-season games and "
        "FP, games share, and roster status (ACTIVE/RESERVE/INJURED_RESERVE/MINORS). Omit team for the "
        "manager's own team.",
        _obj({"team": _STR}),
        get_roster,
    ),
    Tool(
        "get_player",
        "One player's projection and what drives it: FP/GP by stat, this season / last 30 / last 14 days "
        "FP per game, prior sample, model notes. Accepts part of a name or a Fantrax id.",
        _obj({"name": _STR}, ["name"]),
        get_player,
    ),
    Tool(
        "rank_players",
        "League-wide ranking under this league's scoring. available_only=true lists free agents and "
        "waiver players. sort 'ros' (rest-of-season FP, default) or 'fp' (FP per game).",
        _obj(
            {
                "pos": _POS,
                "available_only": {"type": "boolean"},
                "sort": {"type": "string", "enum": ["ros", "fp"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            }
        ),
        rank_players,
    ),
    Tool(
        "best_lineup",
        "Run the exact lineup optimizer for a weekly roster period: starters by slot with games and "
        "expected FP, bench with reasons, moves vs the current lineup, and flags. Pass `out` with players "
        "who won't play (e.g. injured per the news) to re-optimize without them.",
        _obj({"period": _PERIOD, "out": _NAMES}),
        best_lineup,
    ),
    Tool(
        "waiver_options",
        "Best free-agent pickups by rest-of-season gain to the manager's starting lineup (with the best "
        "drop), streamers for the period, the weakest starter vs the best available per position, and "
        "open roster spots. `protect` = players never to drop.",
        _obj(
            {
                "pos": {"type": "string", "enum": ["C", "LW", "RW", "D", "G"]},
                "protect": _NAMES,
                "period": _PERIOD,
            }
        ),
        waiver_options,
    ),
    Tool(
        "evaluate_trade",
        "Evaluate a trade for both sides: change in each starting lineup (rest of season), keeper value "
        "above the keeper line, draft-pick value, roster overflow, and a verdict. give/get are "
        "comma-separated player names (use '-' for none); picks look like '2027:2' (season:round). "
        "partner is needed only when receiving picks alone.",
        _obj(
            {"give": _STR, "get": _STR, "give_picks": _NAMES, "get_picks": _NAMES, "partner": _STR},
            ["give", "get"],
        ),
        evaluate_trade,
    ),
    Tool(
        "keeper_plan",
        "Multi-year keeper value for each player on the manager's roster and the best set of regular + "
        "minors keepers with franchise tags, under the league's keeper rules.",
        _obj({"horizon": {"type": "integer", "minimum": 1, "maximum": 5}}),
        keeper_plan,
    ),
    Tool(
        "league_intel",
        "Every team's starting-lineup strength by slot (ranks, strengths, weaknesses), this week's H2H "
        "opponent, and 1-for-1 trade ideas that help both lineups.",
        _obj(),
        league_intel,
    ),
    Tool(
        "schedule",
        "The NHL schedule for a roster period: its dates and how many games each NHL team plays.",
        _obj({"period": _PERIOD}),
        schedule,
    ),
)
READ_ONLY_TOOLS = tuple(t.name for t in TOOLS)
BY_NAME = {t.name: t for t in TOOLS}


class Executor:
    """Runs tool calls against one LeagueContext. Remembers whether any result used MoneyPuck data."""

    def __init__(self, ctx: LeagueContext):
        self.ctx = ctx
        self.uses_moneypuck = False
        self.calls: list[str] = []

    def definitions(self) -> list[dict]:
        return [t.definition() for t in TOOLS]

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """(JSON result, is_error). Errors go back to the model as text so it can recover."""
        self.calls.append(name)
        tool = BY_NAME.get(name)
        if tool is None:
            return f"Unknown tool {name!r}. Available: {', '.join(READ_ONLY_TOOLS)}", True
        props = tool.schema["properties"]
        unknown = set(args or {}) - set(props)
        if unknown:
            return f"Unknown argument(s) {sorted(unknown)} for {name}", True
        try:
            result = tool.fn(self.ctx, **(args or {}))
        except (ContextError, FetchError) as e:
            return str(e), True
        except (TypeError, ValueError) as e:
            return f"Bad arguments for {name}: {e}", True
        if result.pop("_uses_moneypuck", False):
            self.uses_moneypuck = True
        return json.dumps(result, default=str, separators=(",", ":")), False
