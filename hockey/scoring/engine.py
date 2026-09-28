"""Pure scoring: rules x stat line -> fantasy points, with a per-stat breakdown."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from hockey.scoring.rules import ScoringRules


class MissingStatError(Exception):
    pass


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    parts: dict[str, float]  # canonical stat -> points contributed

    def top(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.parts.items(), key=lambda kv: abs(kv[1]), reverse=True)[:n]


def score(
    rules: ScoringRules, pos_group: str, line: Mapping[str, float], *, strict: bool = True
) -> ScoreBreakdown:
    """Score one stat line (season totals, per-game rates, a single game... any consistent unit).

    With ``strict`` a scored stat missing from ``line`` raises instead of counting as zero, so a
    gap in the data can't masquerade as a bad player.
    """
    weights = rules.for_pos_group(pos_group)
    missing = [s for s, w in weights.items() if w and s not in line]
    if missing and strict:
        raise MissingStatError(f"stat line for {pos_group} is missing scored stats: {sorted(missing)}")
    parts = {s: w * float(line.get(s, 0.0)) for s, w in weights.items() if w}
    return ScoreBreakdown(total=sum(parts.values()), parts=parts)
