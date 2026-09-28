"""Trade analyzer: what a proposed trade does to both teams.

Three components, each reported separately so the verdict is explainable:

1. **Roster fit (this season).** Change in each team's best starting lineup over the rest of the
   season: V(after) - V(before), where V is the exact lineup optimizer with rest-of-season points.
   This is the same measure as `hockey waivers`, so positional need is automatic: a goalie is worth a
   lot to a team with an empty G slot and little to a team that would bench him.
2. **Keeper value (next season).** Next-season points above the keeper line: the value of the
   player ranked (teams x regular keepers), since roughly that many players are kept league-wide.
   Only keeper-worthy players carry it. Acquired players' keeper clocks reset on a trade (league rule).
3. **Draft picks.** A pick is worth the next-season value of the player typically still available
   at that slot (keepers taken + picks before it), above waiver level. The draft order is lottery-based,
   so the slot within a round is taken as mid-round.

Positional scarcity is shown per player as value over replacement at his position (replacement =
the player ranked just past what the league starts at that slot).

Verdict score per side = roster-fit change + keeper_weight x (keeper + pick change).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from hockey.keeper.aging import age_factor
from hockey.keepers import KeeperEntry
from hockey.lineup.optimize import SLOTS
from hockey.lineup.report import LINEUP_STATUSES, eligible_slots
from hockey.valuation import RosterRow
from hockey.waivers.report import open_spots, team_value

SEASON_GAMES = 82
KEEPER_WEIGHT = 0.5
WAIVER_LINE_ROSTER_SPOTS = 18  # Active + Reserve: roughly the players a team carries into a season


class TradeError(Exception):
    pass


@dataclass(frozen=True)
class Pick:
    season: int
    round: int

    @classmethod
    def parse(cls, text: str) -> Pick:
        try:
            season, rnd = text.split(":")
            return cls(int(season), int(rnd))
        except ValueError as e:
            raise TradeError(f"Draft pick {text!r} should look like 2027:2 (season:round)") from e

    def __str__(self) -> str:
        return f"{self.season} round {self.round}"


@dataclass
class Piece:
    """One player in the trade, with the numbers that explain his value."""

    row: RosterRow
    eligible: tuple[str, ...]
    ros: float
    next_season: float
    vor: float  # rest-of-season points over replacement at his position
    keeper_value: float  # next-season points above the keeper line (0 if not keeper-worthy)
    notes: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.row.fantrax_id


@dataclass
class Side:
    team: str
    sends: list[Piece]
    receives: list[Piece]
    picks_sent: list[Pick]
    picks_received: list[Pick]
    lineup_before: float
    lineup_after: float
    keeper_change: float
    pick_change: float
    roster_overflow: int  # players the team would have to drop to stay legal
    score: float = 0.0

    @property
    def lineup_change(self) -> float:
        return self.lineup_after - self.lineup_before


@dataclass
class TradeReport:
    me: Side
    them: Side
    keeper_weight: float
    replacement: dict[str, float]
    keeper_line: float
    pick_values: dict[Pick, float]
    notes: list[str] = field(default_factory=list)
    uses_moneypuck: bool = False

    @property
    def verdict(self) -> str:
        me, them = self.me.score, self.them.score
        if me > 0 and them > 0:
            return "Good for both sides: a realistic offer"
        if me > 0 >= them:
            return "Good for you, bad for them: expect a no unless it fills a need they care about"
        if me <= 0 < them:
            return "Bad for you: they win this one"
        return "Bad for both sides"


def next_season_value(row: RosterRow, age_now: float | None = None) -> float:
    """Next season's projected points; with ``age_now`` (age this season), aged one year."""
    p = row.projection
    if not p:
        return 0.0
    return p.fp_per_gp * p.games_share * SEASON_GAMES * age_factor(age_now, row.pos_group)


def replacement_levels(pool: list[RosterRow], teams: int, slots: dict[str, int]) -> dict[str, float]:
    """ROS points of the first player past what the league starts at each slot."""
    levels = {}
    for pos, n in slots.items():
        values = sorted(
            (r.projection.ros_fp for r in pool if r.projection and pos in eligible_slots(r)), reverse=True
        )
        k = teams * n
        levels[pos] = values[k] if len(values) > k else 0.0
    return levels


def ranked_next_season(pool: list[RosterRow]) -> list[float]:
    return sorted((next_season_value(r) for r in pool if r.projection), reverse=True)


def at_rank(values: list[float], rank: int) -> float:
    """Value of the rank-th best (1-based); 0 past the end."""
    return values[rank - 1] if 0 < rank <= len(values) else 0.0


def pick_value(pick: Pick, values: list[float], teams: int, keepers_per_team: int) -> float:
    kept = teams * keepers_per_team
    rank = kept + (pick.round - 1) * teams + (teams + 1) // 2  # mid-round slot
    waiver_line = at_rank(values, teams * WAIVER_LINE_ROSTER_SPOTS)
    return max(0.0, at_rank(values, rank) - waiver_line)


def analyze(
    mine: list[RosterRow],
    theirs: list[RosterRow],
    give: list[RosterRow],
    get: list[RosterRow],
    pool: list[RosterRow],
    *,
    my_name: str,
    their_name: str,
    roster_cfg: dict,
    teams: int = 10,
    regular_keepers: int = 10,
    give_picks: list[Pick] | None = None,
    get_picks: list[Pick] | None = None,
    keepers: dict[str, KeeperEntry] | None = None,
    keeper_weight: float = KEEPER_WEIGHT,
    ages: dict[int, float] | None = None,
) -> TradeReport:
    give_picks, get_picks, keepers = give_picks or [], get_picks or [], keepers or {}
    slots = roster_cfg.get("active") or SLOTS
    if not (give or give_picks) or not (get or get_picks):
        raise TradeError("A trade needs something going each way (players or picks)")
    mine_ids, their_ids = {r.fantrax_id for r in mine}, {r.fantrax_id for r in theirs}
    for r in give:
        if r.fantrax_id not in mine_ids:
            raise TradeError(f"{r.name} isn't on your roster")
    for r in get:
        if r.fantrax_id not in their_ids:
            raise TradeError(f"{r.name} isn't on {their_name}'s roster")

    replacement = replacement_levels(pool, teams, slots)
    ranked = ranked_next_season(pool)
    keeper_line = at_rank(ranked, teams * regular_keepers)

    ages = ages or {}

    def piece(r: RosterRow) -> Piece:
        nxt = next_season_value(r, ages.get(r.nhl_id) if r.nhl_id else None)
        ros = r.projection.ros_fp if r.projection else 0.0
        elig = eligible_slots(r)
        vor = max((ros - replacement.get(p, 0.0) for p in elig), default=0.0)
        notes = []
        k = keepers.get(r.fantrax_id)
        if k and k.franchise_tag:
            notes.append("franchise-tagged: trading him frees a tag, and it can't go back on him")
        if k and k.times_kept:
            notes.append(f"kept {k.times_kept}x: the clock resets to 0 for the new team")
        return Piece(r, elig, ros, nxt, vor, max(0.0, nxt - keeper_line), notes)

    def lineup(rows: list[RosterRow]) -> float:
        players = [piece(r) for r in rows if (r.status or "").upper() in LINEUP_STATUSES]
        return team_value(players, lambda p: p.ros, slots)

    give_ids, get_ids = {r.fantrax_id for r in give}, {r.fantrax_id for r in get}

    # Incoming players join the receiving team's lineup-eligible group (they arrive on Active/Reserve).
    def arriving(rows: list[RosterRow]) -> list[RosterRow]:
        keep_ir = lambda r: (r.status or "").upper() == "INJURED_RESERVE"  # noqa: E731
        return [replace(r, status=r.status if keep_ir(r) else "RESERVE") for r in rows]

    mine_after = [r for r in mine if r.fantrax_id not in give_ids] + arriving(get)
    theirs_after = [r for r in theirs if r.fantrax_id not in get_ids] + arriving(give)

    give_p, get_p = [piece(r) for r in give], [piece(r) for r in get]
    pick_values = {p: pick_value(p, ranked, teams, regular_keepers) for p in give_picks + get_picks}
    picks_net = sum(pick_values[p] for p in get_picks) - sum(pick_values[p] for p in give_picks)
    keeper_net = sum(p.keeper_value for p in get_p) - sum(p.keeper_value for p in give_p)

    def overflow(before: list[RosterRow], after: list[RosterRow]) -> int:
        net = len(after) - len(before)
        return max(0, net - open_spots(before, roster_cfg)) if net > 0 else 0

    me = Side(
        my_name,
        give_p,
        get_p,
        give_picks,
        get_picks,
        lineup(mine),
        lineup(mine_after),
        keeper_net,
        picks_net,
        overflow(mine, mine_after),
    )
    them = Side(
        their_name,
        get_p,
        give_p,
        get_picks,
        give_picks,
        lineup(theirs),
        lineup(theirs_after),
        -keeper_net,
        -picks_net,
        overflow(theirs, theirs_after),
    )
    for side in (me, them):
        side.score = side.lineup_change + keeper_weight * (side.keeper_change + side.pick_change)

    report = TradeReport(
        me,
        them,
        keeper_weight,
        replacement,
        keeper_line,
        pick_values,
        uses_moneypuck=any(r.projection and r.projection.uses_moneypuck for r in give + get),
    )
    for side in (me, them):
        if side.roster_overflow:
            report.notes.append(
                f"{side.team} would have to drop {side.roster_overflow} player"
                f"{'s' if side.roster_overflow > 1 else ''} to stay under the roster limit"
            )
    if any(p.row.status and p.row.status.upper() == "INJURED_RESERVE" for p in give_p + get_p):
        report.notes.append(
            "An injured (IR) player is involved: his rest-of-season value already reflects fewer "
            "projected games, but check the injury news"
        )
    if give_picks or get_picks:
        report.notes.append(
            "Pick values assume a mid-round slot (the league's draft order follows the NHL lottery)"
        )
    return report
