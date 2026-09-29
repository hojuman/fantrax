"""League intel: every team's strengths and weaknesses, and who makes a good trade partner.

Team strength is the rest-of-season value of its best starting lineup (the same lineup DP used by
lineups, waivers and trades), broken down by slot: the points its starters at C / LW / RW / D / G are
projected to score, ranked across the league.

Trade partners come from 1-for-1 swaps where *both* starting lineups improve: typically a player who
sits on their bench but would start for you, for one of yours who would start for them. Only
Active/Reserve players are considered (IR and Minors players don't affect lineups right now).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hockey.lineup.optimize import SLOTS, Candidate, optimize
from hockey.lineup.report import LINEUP_STATUSES, eligible_slots
from hockey.valuation import RosterRow

STRONG_SHARE = 0.3  # top 30% of the league at a slot = strength, bottom 30% = weakness
CANDIDATES_PER_SIDE = 6  # swaps are searched among each side's 6 most useful players to the other


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    eligible: tuple[str, ...]
    ros: float
    row: RosterRow | None = None


@dataclass
class TeamProfile:
    team_id: str
    name: str
    lineup: float  # best starting lineup, rest-of-season FP
    by_slot: dict[str, float]  # starters' ROS FP per slot type
    empty: dict[str, int]
    bench: list[Asset]
    ranks: dict[str, int] = field(default_factory=dict)  # 1 = best in the league
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    overall_rank: int = 0


@dataclass
class Swap:
    team: str
    give: Asset  # yours
    get: Asset  # theirs
    my_gain: float
    their_gain: float

    @property
    def mutual(self) -> float:
        return min(self.my_gain, self.their_gain)


def assets(rows: list[RosterRow]) -> list[Asset]:
    return [
        Asset(r.fantrax_id, r.name, eligible_slots(r), r.projection.ros_fp if r.projection else 0.0, r)
        for r in rows
        if (r.status or "").upper() in LINEUP_STATUSES
    ]


def lineup_value(players: list[Asset], slots: dict[str, int]) -> float:
    return optimize([Candidate(p.id, p.eligible, max(0.0, p.ros)) for p in players], slots).total


def profile(team_id: str, name: str, players: list[Asset], slots: dict[str, int]) -> TeamProfile:
    a = optimize([Candidate(p.id, p.eligible, max(0.0, p.ros)) for p in players], slots)
    by_id = {p.id: p for p in players}
    by_slot = {pos: 0.0 for pos in slots}
    for pid, pos in a.slot_of.items():
        by_slot[pos] += by_id[pid].ros
    bench = sorted((p for p in players if p.id not in a.slot_of), key=lambda p: -p.ros)
    return TeamProfile(team_id, name, a.total, by_slot, a.empty, bench)


def rank_league(profiles: list[TeamProfile], slots: dict[str, int]) -> None:
    n = len(profiles)
    cut = max(1, round(n * STRONG_SHARE))
    for i, p in enumerate(sorted(profiles, key=lambda p: -p.lineup), 1):
        p.overall_rank = i
    for pos in slots:
        for i, p in enumerate(sorted(profiles, key=lambda p: -p.by_slot[pos]), 1):
            p.ranks[pos] = i
            if i <= cut:
                p.strengths.append(pos)
            elif i > n - cut:
                p.weaknesses.append(pos)


def swaps_with(mine: list[Asset], theirs: list[Asset], team: str, slots: dict[str, int]) -> list[Swap]:
    """1-for-1 swaps that improve both starting lineups, best mutual gain first."""
    base_me, base_them = lineup_value(mine, slots), lineup_value(theirs, slots)
    # Shortlist: their players who'd add most to you, yours who'd add most to them.
    to_me = sorted(theirs, key=lambda a: -(lineup_value([*mine, a], slots) - base_me))[:CANDIDATES_PER_SIDE]
    to_them = sorted(mine, key=lambda a: -(lineup_value([*theirs, a], slots) - base_them))[
        :CANDIDATES_PER_SIDE
    ]
    out = []
    for get in to_me:
        for give in to_them:
            my_gain = lineup_value([p for p in mine if p.id != give.id] + [get], slots) - base_me
            their_gain = lineup_value([p for p in theirs if p.id != get.id] + [give], slots) - base_them
            if my_gain > 0.5 and their_gain > 0.5:
                out.append(Swap(team, give, get, my_gain, their_gain))
    best: dict[str, Swap] = {}  # keep the best swap for each player you'd receive: more varied ideas
    for sw in sorted(out, key=lambda s: (-s.mutual, -s.my_gain)):
        best.setdefault(sw.get.id, sw)
    return list(best.values())


@dataclass
class LeagueIntel:
    me: TeamProfile
    teams: list[TeamProfile]  # everyone, including you, by overall rank
    partners: list[tuple[TeamProfile, list[Swap]]]  # other teams with at least one mutual swap, best first
    opponent: TeamProfile | None = None  # this period's H2H opponent, when known
    uses_moneypuck: bool = False


def build_intel(
    rosters: dict[str, tuple[str, list[RosterRow]]],
    my_team_id: str,
    slots: dict[str, int] | None = None,
    opponent_id: str | None = None,
    max_swaps: int = 3,
) -> LeagueIntel:
    """``rosters``: team_id -> (team name, roster rows)."""
    slots = slots or SLOTS
    team_assets = {tid: assets(rows) for tid, (_, rows) in rosters.items()}
    profiles = [profile(tid, name, team_assets[tid], slots) for tid, (name, _) in rosters.items()]
    rank_league(profiles, slots)
    by_id = {p.team_id: p for p in profiles}
    partners = []
    for tid, (name, _) in rosters.items():
        if tid == my_team_id:
            continue
        found = swaps_with(team_assets[my_team_id], team_assets[tid], name, slots)
        if found:
            partners.append((by_id[tid], found[:max_swaps]))
    partners.sort(key=lambda tp: -tp[1][0].mutual)
    uses_mp = any(r.projection and r.projection.uses_moneypuck for _, rows in rosters.values() for r in rows)
    return LeagueIntel(
        by_id[my_team_id],
        sorted(profiles, key=lambda p: p.overall_rank),
        partners,
        by_id.get(opponent_id) if opponent_id else None,
        uses_mp,
    )


def opponent_for(matchups: list[dict], period: int, team_id: str) -> str | None:
    """Team id of ``team_id``'s H2H opponent in ``period`` (Fantrax getLeagueInfo matchups)."""
    for m in matchups or []:
        if m.get("period") != period:
            continue
        for game in m.get("matchupList") or []:
            home, away = (game.get("home") or {}).get("id"), (game.get("away") or {}).get("id")
            if home == team_id:
                return away
            if away == team_id:
                return home
    return None
