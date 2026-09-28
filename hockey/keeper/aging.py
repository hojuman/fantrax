"""Age curve: expected year-over-year change in fantasy points per game, by age and position.

A heuristic in the shape the public aging research agrees on: forwards improve into their mid-20s,
hold to ~27, then decline, accelerating after 31; defencemen run about a year later; goalies peak
latest and flattest. The factor for age `a` is the multiplier from the season played at age `a` to the
season at `a + 1`. Tune here; everything downstream reads these tables.
"""

from __future__ import annotations

from datetime import date

# (max age inclusive, factor); the last row covers everything older.
CURVES = {
    "F": [(20, 1.10), (22, 1.06), (24, 1.03), (27, 1.00), (29, 0.98), (31, 0.95), (33, 0.92), (99, 0.88)],
    "D": [(21, 1.08), (23, 1.05), (25, 1.02), (28, 1.00), (30, 0.98), (32, 0.95), (34, 0.92), (99, 0.88)],
    "G": [(23, 1.04), (26, 1.02), (30, 1.00), (32, 0.98), (34, 0.95), (99, 0.90)],
}


def age_factor(age: float | None, pos_group: str | None) -> float:
    """Multiplier from this season to the next. Unknown age: no change."""
    if age is None:
        return 1.0
    for max_age, factor in CURVES.get(pos_group or "F", CURVES["F"]):
        if age <= max_age:
            return factor
    return 1.0


def age_on(birth_date: str | None, when: date) -> float | None:
    """Age in years (fractional) on ``when``; None if the birth date is unknown or malformed."""
    if not birth_date:
        return None
    try:
        born = date.fromisoformat(birth_date[:10])
    except ValueError:
        return None
    return (when - born).days / 365.25


def season_start(season_start_year: int) -> date:
    """Ages are measured on Oct 1 of a season's first year (hockey's usual cutoff)."""
    return date(season_start_year, 10, 1)
