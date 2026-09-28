"""Scoring validation: our engine vs Fantrax's own FPts, from one CSV export.

This needs no ID mapping (stats and FPts come from the same Fantrax rows), so it isolates the
question "did we map every scoring code and weight correctly?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hockey.scoring.engine import score
from hockey.scoring.rules import ScoringConfigError, ScoringRules, canonical_stat
from hockey.sources.fantrax_csv import read_players_csv


@dataclass
class CheckResult:
    checked: int = 0
    within: int = 0
    tolerance: float = 0.1
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    worst: list[tuple[str, str, float, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.checked > 0 and self.within == self.checked and not self.missing_columns

    def summary(self) -> dict[str, str]:
        return {
            "players checked": str(self.checked),
            f"within ±{self.tolerance}": str(self.within),
            "result": "PASS" if self.ok else "FAIL",
        }


def csv_line(stats: dict[str, float], group: str) -> dict[str, float]:
    line: dict[str, float] = {}
    for col, v in stats.items():
        try:
            line[canonical_stat(col, group)] = v
        except ScoringConfigError:
            continue  # columns we don't score (e.g. GAA) are fine to ignore here
    return line


def check_csv(path: Path, rules: ScoringRules, tolerance: float = 0.1, top: int = 10) -> CheckResult:
    res = CheckResult(tolerance=tolerance)
    diffs = []
    for p in read_players_csv(path):
        if p.fpts is None or not p.pos_group:
            continue
        group = "goalie" if p.pos_group == "G" else "skater"
        line = csv_line(p.stats, group)
        missing = [s for s, w in rules.for_pos_group(p.pos_group).items() if w and s not in line]
        if missing:
            res.missing_columns.setdefault(p.pos_group, sorted(missing))
            continue
        computed = score(rules, p.pos_group, line).total
        res.checked += 1
        if abs(computed - p.fpts) <= tolerance + 1e-9:
            res.within += 1
        diffs.append((abs(computed - p.fpts), p.name, p.positions, p.fpts, computed))
    diffs.sort(reverse=True)
    res.worst = [(n, pos, f, c) for d, n, pos, f, c in diffs[:top] if d > tolerance]
    return res
