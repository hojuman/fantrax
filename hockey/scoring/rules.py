"""League scoring rules: Fantrax stat codes <-> canonical stat keys.

Canonical keys are what the NHL source produces (see sources/nhl.py). Weights are data: they come
from Fantrax getLeagueInfo, or from data/league.yaml when Fantrax won't give them to us.

Anything we can't map is an error, never a silent zero: a mis-mapped code makes every valuation
downstream quietly wrong, which is worse than failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SKATER_STATS = {
    "gp",
    "g",
    "a",
    "pts",
    "pm",
    "pim",
    "ppg",
    "ppa",
    "ppp",
    "shg",
    "sha",
    "shp",
    "gwg",
    "otg",
    "sog",
    "hit",
    "blk",
    "fow",
    "fol",
    "tk",
    "gv",
    "toi_min",
    "evg",
    "evp",
}
GOALIE_STATS = {"gp", "gs", "w", "l", "otl", "ga", "sv", "sa", "so", "toi_min", "g", "a", "pts", "pim"}

# Normalized Fantrax code/column -> canonical key. Codes are normalized by _norm() first.
_COMMON = {
    "gp": "gp",
    "g": "g",
    "goals": "g",
    "a": "a",
    "assists": "a",
    "pts": "pts",
    "p": "pts",
    "points": "pts",
    "pim": "pim",
    "penaltyminutes": "pim",
    "toi": "toi_min",
    "min": "toi_min",
}
SKATER_ALIASES = {
    **_COMMON,
    "+/-": "pm",
    "pm": "pm",
    "plusminus": "pm",
    "ppg": "ppg",
    "powerplaygoals": "ppg",
    "ppa": "ppa",
    "powerplayassists": "ppa",
    "ppp": "ppp",
    "pppts": "ppp",
    "powerplaypoints": "ppp",
    "shg": "shg",
    "shorthandedgoals": "shg",
    "sha": "sha",
    "shorthandedassists": "sha",
    "shp": "shp",
    "shpts": "shp",
    "shorthandedpoints": "shp",
    "gwg": "gwg",
    "gamewinninggoals": "gwg",
    "otg": "otg",
    "sog": "sog",
    "s": "sog",
    "sh": "sog",
    "shots": "sog",
    "shotsongoal": "sog",
    "hit": "hit",
    "hits": "hit",
    "ht": "hit",
    "blk": "blk",
    "bs": "blk",
    "bks": "blk",
    "blocks": "blk",
    "blockedshots": "blk",
    "fow": "fow",
    "fw": "fow",
    "faceoffswon": "fow",
    "fol": "fol",
    "fl": "fol",
    "faceoffslost": "fol",
    "tk": "tk",
    "tka": "tk",
    "takeaways": "tk",
    "gv": "gv",
    "gva": "gv",
    "giveaways": "gv",
    "evg": "evg",
    "evp": "evp",
    "evpts": "evp",
}
GOALIE_ALIASES = {
    **_COMMON,
    "gs": "gs",
    "gamesstarted": "gs",
    "w": "w",
    "wins": "w",
    "l": "l",
    "losses": "l",
    "otl": "otl",
    "ol": "otl",
    "overtimelosses": "otl",
    "ga": "ga",
    "goalsagainst": "ga",
    "sv": "sv",
    "svs": "sv",
    "saves": "sv",
    "sa": "sa",
    "soga": "sa",
    "shotsagainst": "sa",
    "so": "so",
    "sho": "so",
    "shutouts": "so",
}
# Rate stats can't be weighted per event in a points league; if a league scores one we need a
# deliberate model, so refuse rather than guess.
RATE_STATS = {"gaa", "sv%", "svpct", "savepercentage", "s%", "fo%", "fopct", "atoi", "toi/g"}

GROUPS = ("skater", "goalie", "F", "D")  # F/D are optional per-position overrides of "skater"


class ScoringConfigError(Exception):
    pass


def _norm(code: str) -> str:
    code = code.strip().lower()
    if code in ("+/-", "sv%", "s%", "fo%", "toi/g"):
        return code
    return re.sub(r"[^a-z0-9%]", "", code)


def canonical_stat(code: str, group: str) -> str:
    """Map a Fantrax stat code or CSV column to a canonical key, or raise."""
    n = _norm(code)
    if n in RATE_STATS:
        raise ScoringConfigError(
            f"Rate stat {code!r} is scored; per-event points can't express it. Add a model for it "
            f"or list it under scoring.ignore_codes in data/league.yaml if its weight is 0."
        )
    aliases = GOALIE_ALIASES if group == "goalie" else SKATER_ALIASES
    if n in aliases:
        return aliases[n]
    raise ScoringConfigError(
        f"Unknown {group} stat code {code!r}. Map it under scoring.code_aliases in data/league.yaml."
    )


@dataclass
class ScoringRules:
    """weights[group][canonical_stat] = fantasy points per unit."""

    weights: dict[str, dict[str, float]] = field(default_factory=dict)
    source: str = "unknown"

    def for_pos_group(self, pos_group: str) -> dict[str, float]:
        if pos_group == "G":
            return self.weights.get("goalie", {})
        base = dict(self.weights.get("skater", {}))
        base.update(self.weights.get(pos_group, {}))  # F/D-specific overrides
        return base

    def is_empty(self) -> bool:
        return not any(self.weights.values())

    def to_json(self) -> dict[str, Any]:
        return {"weights": self.weights, "source": self.source}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ScoringRules:
        return cls(weights=data.get("weights", {}), source=data.get("source", "unknown"))


def build_rules(
    raw: dict[str, dict[str, float]],
    *,
    source: str,
    code_aliases: dict[str, str] | None = None,
    ignore_codes: list[str] | None = None,
) -> ScoringRules:
    """Build rules from {group: {fantrax_code: points}}. Collects every problem before raising."""
    code_aliases = {_norm(k): v for k, v in (code_aliases or {}).items()}
    ignore = {_norm(c) for c in (ignore_codes or [])}
    weights: dict[str, dict[str, float]] = {}
    problems: list[str] = []
    for group, codes in raw.items():
        if group not in GROUPS:
            problems.append(f"unknown scoring group {group!r} (expected one of {GROUPS})")
            continue
        valid = GOALIE_STATS if group == "goalie" else SKATER_STATS
        out = weights.setdefault(group, {})
        for code, pts in codes.items():
            n = _norm(code)
            if n in ignore:
                continue
            try:
                key = code_aliases.get(n) or canonical_stat(code, group)
            except ScoringConfigError as e:
                if not pts:
                    continue  # scored at 0: harmless, skip
                problems.append(str(e))
                continue
            if key not in valid:
                problems.append(
                    f"{group} stat {code!r} maps to {key!r}, which the NHL source doesn't provide"
                )
                continue
            out[key] = out.get(key, 0.0) + float(pts)
    if problems:
        raise ScoringConfigError("Scoring config problems:\n  - " + "\n  - ".join(problems))
    return ScoringRules(weights=weights, source=source)


def rules_from_league_yaml(league: dict[str, Any]) -> ScoringRules | None:
    scoring = league.get("scoring") or {}
    raw = {g: scoring[g] for g in GROUPS if scoring.get(g)}
    if not raw:
        return None
    return build_rules(
        raw,
        source="league.yaml",
        code_aliases=scoring.get("code_aliases"),
        ignore_codes=scoring.get("ignore_codes"),
    )
