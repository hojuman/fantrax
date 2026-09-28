"""Build the weekly lineup recommendation for one fantasy team and one roster period."""

from __future__ import annotations

from dataclasses import dataclass, field

from hockey.idmap.normalize import basic
from hockey.lineup.availability import Availability, availability
from hockey.lineup.optimize import SLOTS, Candidate, optimize
from hockey.lineup.periods import Period
from hockey.sources.nhl import Game
from hockey.valuation import RosterRow, Valuer

LINEUP_STATUSES = {"ACTIVE", "RESERVE", ""}  # "" = unknown (CSV import): treat as movable
IR, MINORS = "INJURED_RESERVE", "MINORS"


@dataclass
class PlayerWeek:
    row: RosterRow
    avail: Availability
    fp_per_gp: float
    value: float  # expected fantasy points this period
    eligible: tuple[str, ...]
    slot: str | None = None  # recommended active slot, None = bench

    @property
    def status(self) -> str:
        return (self.row.status or "").upper()


@dataclass
class LineupReport:
    period: Period
    games_in_period: int
    starters: list[PlayerWeek]
    bench: list[PlayerWeek]
    others: list[PlayerWeek]  # IR / Minors
    empty: dict[str, int]
    total: float
    current_total: float
    moves_in: list[PlayerWeek] = field(default_factory=list)
    moves_out: list[PlayerWeek] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    uses_moneypuck: bool = False

    @property
    def gain(self) -> float:
        return self.total - self.current_total


def eligible_slots(row: RosterRow) -> tuple[str, ...]:
    pos = tuple(p.strip().upper() for p in (row.positions or "").split(",") if p.strip().upper() in SLOTS)
    if pos:
        return pos
    return {"G": ("G",), "D": ("D",), "F": ("C", "LW", "RW")}.get(row.pos_group or "", ())


def build_report(
    valuer: Valuer,
    team_id: str,
    period: Period,
    games: list[Game],
    outs: list[str] | None = None,
    slots: dict[str, int] | None = None,
    reserve_max: int = 6,
    ir_max: int = 3,
) -> LineupReport:
    slots = slots or SLOTS
    out_names = {basic(n) for n in outs or []}
    weeks: list[PlayerWeek] = []
    for row in valuer.team_roster(team_id):
        status = (row.status or "").upper()
        reason = None
        if basic(row.name) in out_names:
            reason = "marked out (--out)"
        elif status == IR:
            reason = "in an IR slot"
        av = availability(row.projection, row.nhl_team, games, out_reason=reason)
        fp = row.projection.fp_per_gp if row.projection else 0.0
        weeks.append(PlayerWeek(row, av, fp, fp * av.expected, eligible_slots(row)))

    movable = [w for w in weeks if w.status in LINEUP_STATUSES]
    # Players who are out never start, even into a slot nobody else can fill.
    cands = {w.row.fantrax_id: Candidate(w.row.fantrax_id, w.eligible, max(0.0, w.value)) for w in movable}
    best = optimize([cands[w.row.fantrax_id] for w in movable if not w.avail.out], slots)
    for w in movable:
        w.slot = best.slot_of.get(w.row.fantrax_id)
    # What the lineup Fantrax has right now is worth (out players contribute 0 wherever they sit).
    current = optimize([cands[w.row.fantrax_id] for w in movable if w.status == "ACTIVE"], slots)

    starters = sorted([w for w in movable if w.slot], key=lambda w: (list(slots).index(w.slot), -w.value))
    bench = sorted([w for w in movable if not w.slot], key=lambda w: -w.value)
    others = sorted([w for w in weeks if w.status not in LINEUP_STATUSES], key=lambda w: -w.value)
    report = LineupReport(
        period=period,
        games_in_period=len(games),
        starters=starters,
        bench=bench,
        others=others,
        empty=best.empty,
        total=best.total,
        current_total=current.total,
        moves_in=[w for w in starters if w.status != "ACTIVE"],
        moves_out=[w for w in bench if w.status == "ACTIVE"],
        uses_moneypuck=any(w.row.projection and w.row.projection.uses_moneypuck for w in weeks),
    )
    _flag(report, weeks, slots, reserve_max, ir_max)
    return report


def _flag(
    r: LineupReport, weeks: list[PlayerWeek], slots: dict[str, int], reserve_max: int, ir_max: int
) -> None:
    for pos, n in r.empty.items():
        r.flags.append(f"{n} empty {pos} slot{'s' if n > 1 else ''}: nobody eligible is left to start there")
    for w in r.starters:
        if w.avail.games == 0:
            r.flags.append(f"{w.row.name} starts at {w.slot} but his team has no games this period")
        for f in w.avail.flags:
            r.flags.append(f"{w.row.name}: {f}")
    for w in r.bench:
        if w.avail.out and w.status == "ACTIVE":
            ir_used = sum(1 for x in weeks if x.status == IR)
            r.flags.append(
                f"{w.row.name} is {w.avail.flags[0]}: bench him (Reserve), or IR if he qualifies "
                f"({ir_used}/{ir_max} IR spots used)"
            )
    for w in r.others:
        proj = w.row.projection
        if w.status == IR and proj and proj.games_share > 0.3 and w.avail.flags == ["in an IR slot"]:
            r.flags.append(
                f"{w.row.name} is in IR but looks healthy (plays {proj.games_share:.0%} of games): "
                "activate him? An IR violation makes the roster illegal after one period"
            )
        if w.status == MINORS and w.row.projection:
            open_slot = next((pos for pos in w.eligible if r.empty.get(pos)), None)
            rivals = [s for s in r.starters if s.slot in w.eligible]
            if open_slot and w.value > 0.5:
                r.flags.append(
                    f"Consider promoting {w.row.name} from Minors: {w.value:.1f} projected FP "
                    f"into the empty {open_slot} slot"
                )
            elif rivals and w.value > min(s.value for s in rivals) + 0.5:
                weakest = min(rivals, key=lambda s: s.value)
                r.flags.append(
                    f"Consider promoting {w.row.name} from Minors: {w.value:.1f} projected FP vs "
                    f"{weakest.row.name}'s {weakest.value:.1f} (needs a reserve spot)"
                )
    reserve = len(r.bench)
    ir = sum(1 for w in weeks if w.status == IR)
    if reserve > reserve_max:
        r.flags.append(
            f"{reserve} players on reserve (max {reserve_max}): the roster would be illegal. "
            "Move someone to IR/Minors or drop a player"
        )
    if ir > ir_max:
        r.flags.append(f"{ir} players in IR slots (max {ir_max}): the roster is illegal")
