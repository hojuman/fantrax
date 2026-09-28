"""Fantrax player id -> NHL player id.

This join is the part most likely to break, so it's a cascade of increasingly loose rules, each
result tagged with how it was made:

  0. override      data/id_overrides.csv (always wins)
  0. external_id   an NHL id carried by Fantrax itself, if the probe ever finds one
  1. sticky        a previous match is kept while the names still agree (stops flip-flopping)
  2. exact         basic-normalized name + position group + NHL team
  3. exact_anypos  basic name unique across all positions, same team (position changes)
  4. alias         canonical name (nicknames, suffixes) unique within position group
     alias_team    ... ambiguous, but unique once filtered by team
  5. fuzzy         rapidfuzz >= FUZZY_MIN, unique; flagged in output until confirmed via override
  -  unmatched     logged with a reason and candidates (`hockey ids --unmatched`)
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

from hockey.idmap.normalize import basic, canonical

FUZZY_MIN = 92.0
STICKY_MIN = 85.0
CANDIDATE_MIN = 60.0  # below this, "nearest" names are noise, not hints
CONFIDENCE = {
    "override": 1.0,
    "external_id": 1.0,
    "exact": 0.99,
    "sticky": 0.97,
    "exact_anypos": 0.95,
    "alias": 0.93,
    "alias_team": 0.9,
    "fuzzy": 0.7,
}
EXTERNAL_ID_KEYS = ("nhlId", "nhlPlayerId", "nhl_id")


@dataclass(frozen=True)
class FxRef:
    fantrax_id: str
    name: str
    pos_group: str | None
    team: str | None
    extra: dict = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class NhlRef:
    nhl_id: int
    name: str
    pos_group: str | None
    team: str | None


@dataclass
class Match:
    fantrax_id: str
    nhl_id: int
    method: str
    confidence: float


@dataclass
class Unmatched:
    fantrax_id: str
    reason: str
    candidates: list[tuple[int, str]]


def load_overrides(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {
            r["fantrax_id"].strip(): int(r["nhl_id"])
            for r in csv.DictReader(f)
            if (r.get("fantrax_id") or "").strip() and (r.get("nhl_id") or "").strip()
        }


class Matcher:
    def __init__(self, nhl_players: list[NhlRef]):
        self.by_id = {p.nhl_id: p for p in nhl_players}
        self.by_basic: dict[str, list[NhlRef]] = defaultdict(list)
        self.by_canon: dict[str, list[NhlRef]] = defaultdict(list)
        # position group -> {nhl_id: canonical name}, for the fuzzy tier
        self.fuzzy_pool: dict[str | None, dict[int, str]] = defaultdict(dict)
        for p in nhl_players:
            self.by_basic[basic(p.name)].append(p)
            self.by_canon[canonical(p.name)].append(p)
            self.fuzzy_pool[p.pos_group][p.nhl_id] = canonical(p.name)
            self.fuzzy_pool[None][p.nhl_id] = canonical(p.name)

    def match(
        self,
        fx: FxRef,
        overrides: dict[str, int] | None = None,
        previous: dict[str, int] | None = None,
    ) -> Match | Unmatched:
        def ok(nhl_id: int, method: str) -> Match:
            return Match(fx.fantrax_id, nhl_id, method, CONFIDENCE[method])

        if overrides and fx.fantrax_id in overrides:
            return ok(overrides[fx.fantrax_id], "override")
        for k in EXTERNAL_ID_KEYS:
            v = fx.extra.get(k)
            if v and str(v).isdigit() and int(v) in self.by_id:
                return ok(int(v), "external_id")
        if previous and fx.fantrax_id in previous:
            prev = self.by_id.get(previous[fx.fantrax_id])
            if prev and fuzz.token_sort_ratio(canonical(prev.name), canonical(fx.name)) >= STICKY_MIN:
                return ok(prev.nhl_id, "sticky")

        same_pos = lambda ps: [p for p in ps if p.pos_group == fx.pos_group]  # noqa: E731
        same_team = lambda ps: [p for p in ps if fx.team and p.team == fx.team]  # noqa: E731

        exact = same_team(same_pos(self.by_basic.get(basic(fx.name), [])))
        if len(exact) == 1:
            return ok(exact[0].nhl_id, "exact")

        anypos = self.by_basic.get(basic(fx.name), [])
        if len(anypos) == 1 and same_team(anypos):
            return ok(anypos[0].nhl_id, "exact_anypos")

        alias = same_pos(self.by_canon.get(canonical(fx.name), []))
        if len(alias) == 1:
            return ok(alias[0].nhl_id, "alias")
        if len(alias) > 1:
            narrowed = same_team(alias)
            if len(narrowed) == 1:
                return ok(narrowed[0].nhl_id, "alias_team")
            return Unmatched(
                fx.fantrax_id,
                f"ambiguous: {len(alias)} NHL players named {fx.name!r} at {fx.pos_group}",
                [(p.nhl_id, f"{p.name} {p.team}") for p in alias],
            )

        choices = self.fuzzy_pool.get(fx.pos_group) or {}
        near = process.extract(canonical(fx.name), choices, scorer=fuzz.token_sort_ratio, limit=5)
        scored = [(s, self.by_id[nhl_id]) for _, s, nhl_id in near]
        hits = [(s, p) for s, p in scored if s >= FUZZY_MIN]
        if len(hits) > 1:
            hits = [(s, p) for s, p in hits if fx.team and p.team == fx.team]
        if len(hits) == 1:
            return ok(hits[0][1].nhl_id, "fuzzy")
        reason = "ambiguous fuzzy match" if len(hits) > 1 else "no NHL player with this name"
        plausible = [(p.nhl_id, f"{p.name} {p.team} ({s:.0f})") for s, p in scored[:3] if s >= CANDIDATE_MIN]
        return Unmatched(fx.fantrax_id, reason, plausible)

    def match_all(
        self,
        fx_players: list[FxRef],
        overrides: dict[str, int] | None = None,
        previous: dict[str, int] | None = None,
    ) -> tuple[list[Match], list[Unmatched]]:
        matched, unmatched = [], []
        for fx in fx_players:
            r = self.match(fx, overrides, previous)
            (matched if isinstance(r, Match) else unmatched).append(r)
        # Two Fantrax ids claiming one NHL id means one of them is wrong; demote the weaker one(s).
        by_nhl: dict[int, list[Match]] = defaultdict(list)
        for m in matched:
            by_nhl[m.nhl_id].append(m)
        for nhl_id, ms in by_nhl.items():
            if len(ms) > 1:
                ms.sort(key=lambda m: m.confidence, reverse=True)
                keep_all_top = ms[0].confidence == ms[1].confidence
                losers = ms if keep_all_top else ms[1:]
                for m in losers:
                    matched.remove(m)
                    unmatched.append(
                        Unmatched(
                            m.fantrax_id,
                            f"conflict: NHL id {nhl_id} claimed by {len(ms)} Fantrax players",
                            [(nhl_id, getattr(self.by_id.get(nhl_id), "name", "?"))],
                        )
                    )
        return matched, unmatched
