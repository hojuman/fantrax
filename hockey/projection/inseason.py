"""Phase 2 projection: the Phase 1 baseline as a prior, updated with this season's evidence.

For every stat (per game played):

    posterior = (n0[stat] * prior  +  S) / (n0[stat] + G)

    S = season total + RECENT_BONUS["last30"] * last-30-days total + RECENT_BONUS["last14"] * last-14 total
    G = the same weighting applied to games played

n0 is the stat's stabilization constant in games: shots and hits describe a player after ~10 games,
goals and +/- need far more. With the default bonuses, a game from the last 14 days counts 2x, one
from 15-30 days ago 1.5x, older games 1x.

MoneyPuck adjustments (when its data is there; always credited in output):
  * Shooting luck: every season's goal count is pulled XG_WEIGHT of the way toward individual
    expected goals (ixG) before it's used, because finishing is only partly a repeatable skill.
  * Role: if this season's ice time per game (itself a posterior) differs from the prior's, the prior's
    offensive rates are scaled by the ratio, and power-play rates by the power-play-TOI ratio. That
    way a promotion to PP1 moves the projection before the points arrive.
Goalies: save % is regressed by shots faced (SV_PCT_SHOTS), then SV and GA are derived from the
posterior shots against, so they stay consistent with each other.

Rest of season: games remaining for the player's NHL team x the player's share of team games
(prior share blended with this season's, N_SHARE team games of prior weight).
Players with no NHL history get a conservative ROOKIE_PERCENTILE rate for their position, labelled as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hockey.projection.baseline import Projection
from hockey.scoring.engine import ScoreBreakdown, score
from hockey.scoring.rules import ScoringRules

SEASON_GAMES = 82
RECENT_BONUS = {"last30": 0.5, "last14": 0.5}
XG_WEIGHT = 0.4
SV_PCT_SHOTS = 1500.0
N_SHARE = 20.0
ROOKIE_PERCENTILE = 0.30
ROOKIE_ON_ROSTER_SHARE = 0.5
MIN_GAMES_FOR_ROLE = 3
ROLE_CLIP = (0.8, 1.25)
PP_CLIP = (0.6, 1.6)
N0_PP_TOI = 10  # PP deployment is streakier than overall ice time

N0_SKATER = {
    "toi_min": 5,
    "sog": 10,
    "hit": 8,
    "blk": 10,
    "fow": 10,
    "fol": 10,
    "tk": 20,
    "gv": 20,
    "pim": 30,
    "a": 35,
    "pts": 35,
    "evp": 35,
    "ppa": 40,
    "ppp": 40,
    "g": 50,
    "evg": 50,
    "ppg": 60,
    "pm": 80,
    "shg": 80,
    "sha": 80,
    "shp": 80,
    "gwg": 80,
    "otg": 80,
}
N0_GOALIE = {
    "toi_min": 5,
    "gs": 10,
    "sa": 10,
    "w": 40,
    "l": 40,
    "otl": 60,
    "so": 80,
    "g": 80,
    "a": 80,
    "pts": 80,
    "pim": 80,
}
N0_DEFAULT = 40
OFFENSE = {"g", "a", "pts", "sog", "evg", "evp", "shg", "sha", "shp", "gwg", "otg"}
POWER_PLAY = {"ppg", "ppa", "ppp"}


@dataclass
class Window:
    gp: int
    stats: dict[str, float]

    def rate(self, stat: str) -> float | None:
        return self.stats.get(stat, 0.0) / self.gp if self.gp else None


@dataclass
class ProjectionV2:
    pos_group: str
    method: str  # blend / prior only / rookie default
    rates: dict[str, float]  # posterior per-game rates
    breakdown: ScoreBreakdown  # FP per game by stat
    fp_per_gp: float
    ros_gp: float  # projected games for the rest of the season
    ros_fp: float
    games_share: float  # share of team games the player is expected to play
    prior_rates: dict[str, float] = field(default_factory=dict)  # after xG / role adjustments
    prior_weight: dict[str, float] = field(default_factory=dict)  # n0 / (n0 + G) per stat
    season: Window | None = None
    last30: Window | None = None
    last14: Window | None = None
    prior: Projection | None = None
    notes: list[str] = field(default_factory=list)
    uses_moneypuck: bool = False

    @property
    def sample_gp(self) -> int:
        return (self.prior.sample_gp if self.prior else 0) + (self.season.gp if self.season else 0)


def xg_adjusted_goals(goals: float, ixg: float | None) -> float:
    """Goals pulled XG_WEIGHT of the way toward expected goals."""
    return goals if ixg is None else goals + XG_WEIGHT * (ixg - goals)


def _clip(x: float, lo_hi: tuple[float, float]) -> float:
    return max(lo_hi[0], min(lo_hi[1], x))


def _n0(pos_group: str, stat: str) -> float:
    return float((N0_GOALIE if pos_group == "G" else N0_SKATER).get(stat, N0_DEFAULT))


def blend(
    rules: ScoringRules,
    pos_group: str,
    *,
    prior: Projection | None,
    prior_rates: dict[str, float],
    season: Window | None = None,
    last30: Window | None = None,
    last14: Window | None = None,
    season_ixg: float | None = None,
    pp_toi_prior: float | None = None,
    pp_toi_season: float | None = None,
    team_gp: int = 0,
    prior_share: float,
    method: str = "blend",
    notes: list[str] | None = None,
    uses_moneypuck: bool = False,
) -> ProjectionV2:
    """Combine a prior (per-game rates) with this season's evidence. See the module docstring."""
    notes = list(notes or [])
    prior_rates = dict(prior_rates)
    windows = {"last30": last30, "last14": last14}

    # Effective in-season evidence: season + recency bonus.
    sums: dict[str, float] = {}
    games = 0.0
    if season and season.gp:
        season_stats = dict(season.stats)
        if season_ixg is not None and pos_group != "G":
            adj = xg_adjusted_goals(season_stats.get("g", 0.0), season_ixg)
            notes.append(
                f"this season: {season_stats.get('g', 0):.0f} G on {season_ixg:.1f} ixG"
                + (
                    " (running hot)"
                    if season_stats.get("g", 0) > season_ixg + 2
                    else " (running cold)"
                    if season_stats.get("g", 0) < season_ixg - 2
                    else ""
                )
            )
            season_stats["pts"] = season_stats.get("pts", 0.0) + adj - season_stats.get("g", 0.0)
            season_stats["g"] = adj
        for k, v in season_stats.items():
            sums[k] = sums.get(k, 0.0) + v
        games += season.gp
        for name, w in windows.items():
            if w and w.gp:
                for k, v in w.stats.items():
                    sums[k] = sums.get(k, 0.0) + RECENT_BONUS[name] * v
                games += RECENT_BONUS[name] * w.gp

    # Role change: scale the prior by this season's (posterior) ice time vs the prior's.
    if season and season.gp >= MIN_GAMES_FOR_ROLE and pos_group != "G" and prior_rates.get("toi_min"):
        n0 = _n0(pos_group, "toi_min")
        toi_post = (n0 * prior_rates["toi_min"] + sums.get("toi_min", 0.0)) / (n0 + games)
        factor = _clip(toi_post / prior_rates["toi_min"], ROLE_CLIP)
        pp_factor = factor
        if pp_toi_prior and pp_toi_season is not None:
            pp_post = (N0_PP_TOI * pp_toi_prior + pp_toi_season * season.gp) / (N0_PP_TOI + season.gp)
            pp_factor = _clip(pp_post / pp_toi_prior, PP_CLIP)
        if abs(factor - 1) >= 0.03:
            notes.append(
                f"ice time {toi_post - prior_rates['toi_min']:+.1f} min/GP vs prior → offense ×{factor:.2f}"
            )
        if abs(pp_factor - 1) >= 0.05 and pp_factor != factor:
            notes.append(f"power-play time → PP stats ×{pp_factor:.2f}")
        for k in OFFENSE:
            if k in prior_rates:
                prior_rates[k] *= factor
        for k in POWER_PLAY:
            if k in prior_rates:
                prior_rates[k] *= pp_factor

    keys = set(prior_rates) | set(sums)
    keys.discard("gp")
    rates: dict[str, float] = {}
    prior_weight: dict[str, float] = {}
    for k in keys:
        n0 = _n0(pos_group, k)
        rates[k] = (n0 * prior_rates.get(k, 0.0) + sums.get(k, 0.0)) / (n0 + games)
        prior_weight[k] = n0 / (n0 + games)

    if pos_group == "G" and rates.get("sa"):
        p_sa, p_sv = prior_rates.get("sa", 0.0), prior_rates.get("sv", 0.0)
        prior_svp = p_sv / p_sa if p_sa else 0.9
        sv_pct = (SV_PCT_SHOTS * prior_svp + sums.get("sv", 0.0)) / (SV_PCT_SHOTS + sums.get("sa", 0.0))
        rates["sv"] = rates["sa"] * sv_pct
        rates["ga"] = rates["sa"] * (1 - sv_pct)
        prior_weight["sv"] = prior_weight["ga"] = SV_PCT_SHOTS / (SV_PCT_SHOTS + sums.get("sa", 0.0))
        rates["sv_pct"] = sv_pct
    rates["gp"] = 1.0

    share = prior_share
    if team_gp:
        played = season.gp if season else 0
        share = (N_SHARE * prior_share + played) / (N_SHARE + team_gp)
        if played == 0 and prior_share > 0:
            notes.append(
                f"0 GP in his team's {team_gp} games so far (injury or scratch?): fewer games projected"
            )
    share = max(0.0, min(1.0, share))
    ros_gp = share * max(0, SEASON_GAMES - team_gp)
    bd = score(rules, pos_group, rates, strict=False)
    return ProjectionV2(
        pos_group=pos_group,
        method=method,
        rates=rates,
        breakdown=bd,
        fp_per_gp=bd.total,
        ros_gp=ros_gp,
        ros_fp=bd.total * ros_gp,
        games_share=share,
        prior_rates=prior_rates,
        prior_weight=prior_weight,
        season=season,
        last30=last30,
        last14=last14,
        prior=prior,
        notes=notes,
        uses_moneypuck=uses_moneypuck,
    )


def rookie_rates(means: dict[str, float], mean_fp: float, target_fp: float) -> dict[str, float]:
    """Position-mean rates scaled down so they're worth ``target_fp`` per game."""
    scale = target_fp / mean_fp if mean_fp > 0 else 0.0
    return {k: v * scale for k, v in means.items()}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    i = p * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)
