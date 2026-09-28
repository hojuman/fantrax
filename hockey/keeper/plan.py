"""Keeper valuation and the best keeper set under Talladega Nights' rules.

Rules (league rules text + Fantrax settings, see data/league.yaml):
  * 10 regular + 5 minors keepers. Minors = career + current regular-season GP <= 165.
  * A regular player can be kept at most 3 times; the clock doesn't run while he's minors-eligible,
    and it resets to 0 when he's traded.
  * 4 franchise tags exempt players from the limit. A tag removed from a player can never go back on
    him, and removing it from someone kept 3+ times makes him unkeepable.

Value of keeping a player = projected fantasy points for each future season he can be kept (up to the
horizon), aged with the age curve and discounted per year:
  * regular slot: points above the keeper line (what the ~100th player gives you: roughly what the draft
    replaces a keeper with), so only keeper-worthy seasons count;
  * minors slot: raw points, since a minors slot competes only with other prospects.
Minors eligibility is judged at keeper time: career GP now + projected rest-of-season GP.

The best set is found exactly by DP over (regular used, tags used, minors used).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hockey.keeper.aging import age_factor
from hockey.keepers import KeeperEntry
from hockey.valuation import RosterRow

HORIZON = 3
DISCOUNT = 0.85
SEASON_GAMES = 82
PROSPECT_NEXT_SHARE = 0.3  # a minors-eligible player not yet playing much: ~25 games next season


@dataclass
class Rules:
    regular: int = 10
    minors: int = 5
    tags: int = 4
    max_times_kept: int = 3
    minors_gp: int = 165

    @classmethod
    def from_league(cls, league: dict) -> Rules:
        k = league.get("keepers") or {}
        return cls(
            int(k.get("regular", 10)),
            int(k.get("minors", 5)),
            int(k.get("franchise_tags", 4)),
            int(k.get("max_times_kept", 3)),
            int(k.get("minors_gp_threshold", 165)),
        )


@dataclass
class Candidate:
    row: RosterRow
    age: float | None  # at the start of next season
    career_gp: int  # now
    career_gp_end: float  # projected at keeper time (end of this season)
    known_career: bool  # False: no NHL record (assumed 0 GP, age unknown)
    entry: KeeperEntry  # from keepers.yaml (defaults if absent)
    in_yaml: bool
    yearly: list[float]  # projected points, next season onward
    regular_untagged: float | None  # None = can't be kept this way
    regular_tagged: float | None
    minors: float | None
    choice: str = "release"  # release / regular / regular+tag / minors
    notes: list[str] = field(default_factory=list)

    @property
    def minors_eligible(self) -> bool:
        return self.minors is not None

    @property
    def value(self) -> float:
        return {
            "regular": self.regular_untagged,
            "regular+tag": self.regular_tagged,
            "minors": self.minors,
        }.get(self.choice) or 0.0


@dataclass
class KeeperPlan:
    candidates: list[Candidate]
    rules: Rules
    keeper_line: float
    total: float
    warnings: list[str] = field(default_factory=list)
    uses_moneypuck: bool = False

    def chosen(self, kind: str) -> list[Candidate]:
        return [c for c in self.candidates if c.choice.startswith(kind)]


def yearly_values(
    current_season_value: float, age_next: float | None, pos_group: str | None, horizon: int = HORIZON
) -> list[float]:
    """Projected points for next season onward: this season's value aged forward a year at a time."""
    out, v = [], current_season_value
    age = None if age_next is None else age_next - 1  # the age this season was played at
    for y in range(horizon):
        v *= age_factor(None if age is None else age + y, pos_group)
        out.append(v)
    return out


def discounted(values: list[float], years: int, line: float = 0.0) -> float:
    return sum(DISCOUNT**y * max(0.0, v - line) for y, v in enumerate(values[:years]))


def build_candidate(
    row: RosterRow,
    *,
    age_next: float | None,
    career_gp: int,
    known_career: bool,
    entry: KeeperEntry | None,
    keeper_line: float,
    rules: Rules,
    horizon: int = HORIZON,
) -> Candidate:
    p = row.projection
    ros_gp = p.ros_gp if p else 0.0
    gp_end = career_gp + ros_gp
    minors_ok = gp_end <= rules.minors_gp
    share = p.games_share if p else 0.0
    if minors_ok:
        share = max(share, PROSPECT_NEXT_SHARE)
    season_value = (p.fp_per_gp if p else 0.0) * share * SEASON_GAMES
    years = yearly_values(season_value, age_next, row.pos_group, horizon)
    e = entry or KeeperEntry(row.fantrax_id, row.name)
    keeps_left = max(0, rules.max_times_kept - e.times_kept)

    can_tag = not e.tag_removed
    untagged_years = horizon if minors_ok else min(horizon, keeps_left)
    if e.franchise_tag and not minors_ok and keeps_left == 0:
        untagged_years = 0  # removing his tag at the limit makes him unkeepable
    c = Candidate(
        row=row,
        age=age_next,
        career_gp=career_gp,
        career_gp_end=gp_end,
        known_career=known_career,
        entry=e,
        in_yaml=entry is not None,
        yearly=years,
        regular_untagged=discounted(years, untagged_years, keeper_line) if untagged_years > 0 else None,
        regular_tagged=discounted(years, horizon, keeper_line) if can_tag else None,
        minors=discounted(years, horizon) if minors_ok else None,
    )
    if career_gp <= rules.minors_gp < gp_end:
        c.notes.append(
            f"graduates from minors this season ({career_gp} GP now, ~{gp_end:.0f} by keeper time)"
        )
    if not known_career:
        c.notes.append("no NHL record: assumed 0 career GP (minors-eligible), age unknown")
    if not minors_ok and keeps_left == 0 and not e.franchise_tag:
        c.notes.append(
            "kept 3 times: needs a franchise tag to be kept again"
            + (" (but a tag was removed from him before: not allowed)" if e.tag_removed else "")
        )
    return c


def choose(cands: list[Candidate], rules: Rules) -> float:
    """Exact best keeper set. Sets each candidate's ``choice``; returns the total value."""
    # state: (regular, tags, minors) -> (score, back-state, option)
    layers: list[dict[tuple, tuple]] = [{(0, 0, 0): (0.0, None, "release")}]
    for c in cands:
        prev, cur = layers[-1], {}
        for (reg, tags, mn), (score, _, _) in prev.items():
            options = [("release", (reg, tags, mn), 0.0)]
            if c.minors is not None and mn < rules.minors:
                options.append(("minors", (reg, tags, mn + 1), c.minors))
            if c.regular_untagged is not None and reg < rules.regular:
                options.append(("regular", (reg + 1, tags, mn), c.regular_untagged))
            if c.regular_tagged is not None and reg < rules.regular and tags < rules.tags:
                options.append(("regular+tag", (reg + 1, tags + 1, mn), c.regular_tagged))
            for name, state, gain in options:
                # Prefer not spending a tag when it adds nothing; small bonus for keeping existing tags.
                tie = 1e-6 if (name == "regular+tag") == c.entry.franchise_tag else 0.0
                new = score + gain + tie
                if state not in cur or new > cur[state][0] + 1e-12:
                    cur[state] = (new, (reg, tags, mn), name)
        layers.append(cur)
    final = layers[-1]
    state = max(final, key=lambda s: final[s][0])
    total = 0.0
    for i in range(len(cands), 0, -1):
        _, back, name = layers[i][state]
        cands[i - 1].choice = name
        total += cands[i - 1].value
        state = back
    return total


def plan(candidates: list[Candidate], rules: Rules, keeper_line: float) -> KeeperPlan:
    total = choose(candidates, rules)
    p = KeeperPlan(
        candidates,
        rules,
        keeper_line,
        total,
        uses_moneypuck=any(c.row.projection and c.row.projection.uses_moneypuck for c in candidates),
    )
    for c in candidates:
        if c.entry.franchise_tag and c.choice != "regular+tag":
            if c.choice == "regular":
                p.warnings.append(
                    f"Moving the franchise tag off {c.row.name} is permanent: it can never go back on him"
                )
            elif c.choice == "release":
                p.warnings.append(f"{c.row.name} is franchise-tagged but not worth keeping: his tag frees up")

    missing = [
        c.row.name
        for c in candidates
        if not c.in_yaml and not c.minors_eligible and c.choice.startswith("regular")
    ]
    if missing:
        p.warnings.append(f"Not in data/keepers.yaml, so assumed never kept by you: {', '.join(missing)}")
    if len(p.chosen("regular")) < rules.regular:
        p.warnings.append(
            f"Only {len(p.chosen('regular'))} of {rules.regular} regular keeper slots are worth using: "
            "everyone else on your roster is below the keeper line"
        )
    return p
