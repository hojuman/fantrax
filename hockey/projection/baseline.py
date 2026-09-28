"""Phase 1 baseline projection: Marcel-style weighted per-game rates, regressed toward the mean.

    rate[stat] = (sum_s w_s * stat_s  +  k * mean_rate[stat]) / (sum_s w_s * gp_s  +  k)

where w = season weights (most recent first, default 5/4/3, normalized so the most recent season
counts 1.0; that keeps k in real games) and k = "phantom games" of an average player at the same
position group. A 10-GP call-up is pulled hard toward average; an 82-GP x 3
veteran barely moves. Fantasy points per game come from scoring those rates with the league's own
weights, so the breakdown shows exactly which stats drive a player's value.

Deliberately simple; Phase 2 adds in-season blending, recent form and MoneyPuck role inputs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from hockey.scoring.engine import ScoreBreakdown, score
from hockey.scoring.rules import ScoringRules
from hockey.sources.nhl import SeasonLine

DEFAULT_WEIGHTS = (5.0, 4.0, 3.0)
DEFAULT_K = {"F": 25.0, "D": 25.0, "G": 20.0}
DEFAULT_GP_CAP = {"F": 82, "D": 82, "G": 65}
MEAN_MIN_GP = 20
NON_RATE = {"gp"}


@dataclass
class Projection:
    nhl_id: int
    pos_group: str
    sample_gp: int  # raw GP across the seasons used
    weighted_gp: float  # sum w_s * gp_s (the "evidence" in the regression)
    shrink: float  # share of the rate that came from the position mean (0..1)
    rates: dict[str, float]  # regressed per-game rates
    proj_gp: float
    fp_per_gp: float
    fp_season: float
    breakdown: ScoreBreakdown  # per-game points by stat


def position_means(lines: list[SeasonLine], season: int) -> dict[str, dict[str, float]]:
    """GP-weighted mean per-game rates by position group, from regulars in one season."""
    tot: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    gp: dict[str, float] = defaultdict(float)
    for ln in lines:
        if ln.season != season or ln.gp < MEAN_MIN_GP:
            continue
        gp[ln.pos_group] += ln.gp
        for k, v in ln.stats.items():
            if k not in NON_RATE:
                tot[ln.pos_group][k] += v
    return {g: {k: v / gp[g] for k, v in stats.items()} for g, stats in tot.items() if gp[g]}


def project_player(
    lines: list[SeasonLine],
    seasons: list[int],
    means: dict[str, dict[str, float]],
    rules: ScoringRules,
    *,
    weights: tuple[float, ...] = DEFAULT_WEIGHTS,
    k: dict[str, float] | None = None,
    gp_cap: dict[str, int] | None = None,
) -> Projection | None:
    """Project one player from their season lines. ``seasons`` is most-recent-first."""
    k = k or DEFAULT_K
    gp_cap = gp_cap or DEFAULT_GP_CAP
    weights = tuple(w / weights[0] for w in weights)
    by_season = {ln.season: ln for ln in lines}
    used = [
        (w, by_season[s])
        for w, s in zip(weights, seasons, strict=False)
        if s in by_season and by_season[s].gp > 0
    ]
    if not used:
        return None
    pos = used[0][1].pos_group
    mean = means.get(pos, {})
    kk = k.get(pos, 25.0)
    wgp = sum(w * ln.gp for w, ln in used)
    keys = set(mean) | {s for _, ln in used for s in ln.stats if s not in NON_RATE}
    rates = {
        s: (sum(w * ln.stats.get(s, 0.0) for w, ln in used) + kk * mean.get(s, 0.0)) / (wgp + kk)
        for s in keys
    }
    rates["gp"] = 1.0
    proj_gp = min(float(gp_cap.get(pos, 82)), sum(w * ln.gp for w, ln in used) / sum(w for w, _ in used))
    bd = score(rules, pos, rates, strict=False)
    return Projection(
        nhl_id=used[0][1].nhl_id,
        pos_group=pos,
        sample_gp=sum(ln.gp for _, ln in used),
        weighted_gp=wgp,
        shrink=kk / (wgp + kk),
        rates=rates,
        proj_gp=proj_gp,
        fp_per_gp=bd.total,
        fp_season=bd.total * proj_gp,
        breakdown=bd,
    )
