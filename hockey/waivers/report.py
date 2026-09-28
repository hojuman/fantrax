"""Waiver / free-agent report.

A pickup's worth is how much it improves the lineup you'd actually start:

    gain = V(roster - drop + pickup) - V(roster)      (no drop when there's an open roster spot)

where V is the exact lineup optimizer's total, with each player's value being either rest-of-season
fantasy points (season-long pickups) or expected points in the next roster period (streamers). That
makes positional need automatic: filling an empty LW slot is worth a lot, a fourth C who'd sit on the
bench is worth ~0, and multi-position players count wherever they help most.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from hockey.idmap.normalize import basic
from hockey.keepers import KeeperEntry
from hockey.lineup.availability import Availability, availability
from hockey.lineup.optimize import SLOTS, Candidate, optimize
from hockey.lineup.periods import Period
from hockey.lineup.report import LINEUP_STATUSES, eligible_slots
from hockey.sources.nhl import Game
from hockey.valuation import RosterRow, Valuer

SHORTLIST = 40
STREAM_MAX_ROS_COST = 10.0
DEPTH_SHOWN = 5  # a one-week stream may cost at most this many rest-of-season points
LOWEST_OVERALL = 3
FREE_AGENT = ("FA", "W")


@dataclass
class Player:
    row: RosterRow
    eligible: tuple[str, ...]
    ros: float  # rest-of-season fantasy points
    week: float  # expected fantasy points in the period
    avail: Availability

    @property
    def id(self) -> str:
        return self.row.fantrax_id

    @property
    def status(self) -> str:
        return (self.row.status or "").upper()


@dataclass
class Option:
    pickup: Player
    drop: Player | None
    gain: float  # in the value being optimized (ROS or week)
    ros_change: float = 0.0  # the same swap measured in rest-of-season points


@dataclass
class PositionRow:
    pos: str
    weakest: Player | None  # your weakest starter there (None = empty slot)
    best: Option | None  # the best free agent eligible there


@dataclass
class WaiverReport:
    period: Period
    open_spots: int
    pickups: list[Option]
    by_position: list[PositionRow]
    streamers: list[Option]
    density: dict[int, list[str]]
    notes: list[str] = field(default_factory=list)
    uses_moneypuck: bool = False
    depth: list[Player] = field(default_factory=list)  # best bench adds for an open spot


def team_value(players: list[Player], value: Callable[[Player], float], slots: dict[str, int]) -> float:
    return optimize([Candidate(p.id, p.eligible, max(0.0, value(p))) for p in players], slots).total


def open_spots(mine: list[RosterRow], roster_cfg: dict) -> int:
    """Spots for a pickup: under the effective roster max, and room on Active + Reserve."""
    effective = int(roster_cfg.get("effective_max") or roster_cfg.get("max_players") or 26)
    active = sum((roster_cfg.get("active") or SLOTS).values())
    lineup_room = active + int(roster_cfg.get("reserve", 6))
    in_lineup = sum(1 for r in mine if (r.status or "").upper() in LINEUP_STATUSES)
    return max(0, min(effective - len(mine), lineup_room - in_lineup))


def drop_candidates(droppable: list[Player], value: Callable[[Player], float]) -> list[Player]:
    """The few players worth trying as drops: the lowest overall plus the lowest per position group."""
    ranked = sorted(droppable, key=value)
    picks = {p.id: p for p in ranked[:LOWEST_OVERALL]}
    by_group: dict[str, Player] = {}
    for p in ranked:
        by_group.setdefault(p.row.pos_group or "?", p)
    picks.update({p.id: p for p in by_group.values()})
    return list(picks.values())


def best_options(
    lineup: list[Player],
    droppable: list[Player],
    pool: list[Player],
    value: Callable[[Player], float],
    slots: dict[str, int],
    spots: int,
    max_ros_cost: float | None = None,
) -> list[Option]:
    """For each pool player, the best swap (or plain add when there's room).

    With ``max_ros_cost`` (streaming), swaps that lose more rest-of-season value than that are skipped.
    """
    ros = lambda p: p.ros  # noqa: E731
    base, base_ros = team_value(lineup, value, slots), team_value(lineup, ros, slots)
    drops = drop_candidates(droppable, ros)  # never consider cutting someone for one week unless he's low ROS
    options = []
    for fa in pool:
        if spots > 0:
            new = [*lineup, fa]
            options.append(
                Option(fa, None, team_value(new, value, slots) - base, team_value(new, ros, slots) - base_ros)
            )
            continue
        best: Option | None = None
        for d in drops:
            new = [p for p in lineup if p.id != d.id] + [fa]
            opt = Option(fa, d, team_value(new, value, slots) - base, team_value(new, ros, slots) - base_ros)
            if max_ros_cost is not None and opt.ros_change < -max_ros_cost:
                continue
            if best is None or (opt.gain, opt.ros_change) > (best.gain, best.ros_change):
                best = opt
        if best:
            options.append(best)
    return sorted(options, key=lambda o: (o.gain, o.ros_change), reverse=True)


def schedule_density(games: list[Game]) -> dict[int, list[str]]:
    counts: dict[str, int] = defaultdict(int)
    for g in games:
        for t in g.teams():
            counts[t] += 1
    out: dict[int, list[str]] = defaultdict(list)
    for team, n in sorted(counts.items()):
        out[n].append(team)
    return dict(sorted(out.items(), reverse=True))


def build_waivers(
    valuer: Valuer,
    team_id: str,
    period: Period,
    games: list[Game],
    roster_cfg: dict,
    keepers: dict[str, KeeperEntry] | None = None,
    protect: list[str] | None = None,
    pos: str | None = None,
    limit: int = 10,
    max_ros_cost: float = STREAM_MAX_ROS_COST,
) -> WaiverReport:
    keepers = keepers or {}
    slots = roster_cfg.get("active") or SLOTS
    protected_names = {basic(n) for n in protect or []}

    def wrap(r: RosterRow) -> Player:
        av = availability(r.projection, r.nhl_team, games, out_reason=None)
        fp = r.projection.fp_per_gp if r.projection else 0.0
        ros = r.projection.ros_fp if r.projection else 0.0
        return Player(r, eligible_slots(r), ros, fp * av.expected, av)

    mine_rows = valuer.team_roster(team_id)
    mine = [wrap(r) for r in mine_rows]
    lineup = [p for p in mine if p.status in LINEUP_STATUSES]
    droppable = [
        p
        for p in lineup
        if not keepers.get(p.id, KeeperEntry(p.id, "")).franchise_tag
        and basic(p.row.name) not in protected_names
    ]

    free = [r for r in valuer.league_players() if r.owner in FREE_AGENT]
    no_record = [r for r in free if not r.nhl_id]
    pool = [wrap(r) for r in free if r.nhl_id and r.projection]
    if pos:
        pool = [p for p in pool if pos in p.eligible]
    season_pool = sorted([p for p in pool if p.row.projection.ros_gp > 0], key=lambda p: -p.ros)[:SHORTLIST]
    week_pool = sorted([p for p in pool if p.week > 0], key=lambda p: -p.week)[:SHORTLIST]

    spots = open_spots(mine_rows, roster_cfg)
    pickups = best_options(lineup, droppable, season_pool, lambda p: p.ros, slots, spots)
    streams = best_options(lineup, droppable, week_pool, lambda p: p.week, slots, spots, max_ros_cost)
    held_back = (
        best_options(lineup, droppable, week_pool, lambda p: p.week, slots, spots) if not spots else []
    )

    # By position: your weakest starter vs the best free agent who can play there.
    assign = optimize([Candidate(p.id, p.eligible, max(0.0, p.ros)) for p in lineup], slots)
    starters = {p.id: p for p in lineup}
    rows = []
    for slot in (pos,) if pos else tuple(slots):
        at_slot = [starters[k] for k, s in assign.slot_of.items() if s == slot]
        weakest = None if assign.empty.get(slot) else min(at_slot, key=lambda p: p.ros, default=None)
        best = next((o for o in pickups if slot in o.pickup.eligible and o.gain > 0.05), None)
        rows.append(PositionRow(slot, weakest, best))

    report = WaiverReport(
        period=period,
        open_spots=spots,
        pickups=[o for o in pickups if o.gain > 0.05][:limit],
        by_position=rows,
        streamers=[o for o in streams if o.gain > 0.05][:limit],
        density=schedule_density(games),
        uses_moneypuck=any(p.row.projection and p.row.projection.uses_moneypuck for p in mine + pool),
    )
    if spots and not report.pickups:
        # Nobody improves the starting lineup, but a free roster spot still has a best use: depth.
        report.depth = season_pool[:DEPTH_SHOWN]
    if no_record:
        report.notes.append(
            f"{len(no_record)} free agents have no NHL record (juniors, Europeans, undrafted "
            "prospects) and are left out; undrafted players can't be claimed in this league anyway"
        )
    for o in report.pickups + report.streamers:
        if o.drop and keepers.get(o.drop.id, KeeperEntry(o.drop.id, "")).times_kept:
            report.notes.append(
                f"{o.drop.row.name} has keeper history in data/keepers.yaml; think twice before dropping him"
            )
    if any(o.pickup.row.owner == "W" for o in report.pickups + report.streamers):
        report.notes.append("Players marked W are on waivers: you'd put in a claim, not an instant add")
    shown = {o.pickup.id for o in report.streamers}
    skipped = [o for o in held_back if o.gain > 0.05 and o.pickup.id not in shown]
    if skipped:
        o = skipped[0]
        report.notes.append(
            f"{len(skipped)} streamer{'s' if len(skipped) > 1 else ''} held back because the only drop would cost more "
            f"than {max_ros_cost:g} rest-of-season points (e.g. {o.pickup.row.name} for {o.drop.row.name}: "
            f"{o.gain:+.1f} this week, {o.ros_change:+.0f} ROS). Raise --max-ros-cost to see them"
        )
    if spots:
        report.notes.append(
            f"You have {spots} open roster spot{'s' if spots > 1 else ''}: pickups need no drop"
        )
    report.notes = list(dict.fromkeys(report.notes))
    return report
