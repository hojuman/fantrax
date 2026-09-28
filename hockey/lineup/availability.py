"""Who plays how many games this period.

There is no injury feed (Fantrax's published API has none and scraping injury sites is off the
table), so availability comes from: an IR slot on Fantrax, `--out NAME` on the command line, and the
projection's games share, which already drops when a player sits while his team plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from hockey.projection.inseason import ProjectionV2
from hockey.sources.nhl import Game

B2B_GAP = timedelta(hours=30)
B2B_STARTER_SHARE = 0.6  # a goalie above this start share usually sits one night of a back-to-back
B2B_FACTOR = 0.5


@dataclass
class Availability:
    games: int  # team games in the period
    expected: float  # expected games played (skaters) / starts (goalies)
    b2b: int = 0  # back-to-back second nights in the period
    out: bool = False
    flags: list[str] = field(default_factory=list)


def team_games(games: list[Game], team: str | None) -> list[Game]:
    return [g for g in games if team and team in g.teams()] if team else []


def availability(
    proj: ProjectionV2 | None, team: str | None, games: list[Game], *, out_reason: str | None
) -> Availability:
    mine = team_games(games, team)
    b2b_idx = [i for i in range(1, len(mine)) if mine[i].start_utc - mine[i - 1].start_utc <= B2B_GAP]
    av = Availability(games=len(mine), expected=0.0, b2b=len(b2b_idx))
    if out_reason:
        av.out = True
        av.flags.append(out_reason)
        return av
    if proj is None:
        av.flags.append("no projection")
        return av
    for n in proj.notes:
        if "0 GP in his team's" in n:
            av.flags.append("hasn't played lately (injury/scratch?)")
    if proj.pos_group == "G":
        start_share = min(1.0, proj.games_share * proj.rates.get("gs", 1.0))
        per_game = [start_share] * len(mine)
        if start_share > B2B_STARTER_SHARE:
            for i in b2b_idx:
                per_game[i] = start_share * B2B_FACTOR
        av.expected = sum(per_game)
    else:
        av.expected = proj.games_share * len(mine)
    return av
