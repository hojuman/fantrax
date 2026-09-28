"""Fantrax roster periods (weekly lineup locks), from getLeagueInfo `rosterPeriods` stored in meta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Period:
    number: int
    start: datetime  # the lineup lock: lineups must be set before this
    end: datetime

    def contains(self, t: datetime) -> bool:
        return self.start <= t < self.end


def _ts(value: str) -> datetime:
    # e.g. "2026-10-05T19:00:00.0-0400"
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def parse_periods(raw: list[dict]) -> list[Period]:
    out = []
    for p in raw or []:
        try:
            out.append(Period(int(p["number"]), _ts(p["startDate"]), _ts(p["endDate"])))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda p: p.number)


def next_period(periods: list[Period], now: datetime) -> Period | None:
    """The next lineup lock: the first period that hasn't started yet."""
    return next((p for p in periods if p.start > now), None)


def current_period(periods: list[Period], now: datetime) -> Period | None:
    return next((p for p in periods if p.contains(now)), None)
