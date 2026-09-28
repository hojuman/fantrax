"""Phase 2 projection model: MoneyPuck parsing and the in-season blend, in isolation."""

import pytest

from hockey.projection.baseline import Projection
from hockey.projection.inseason import (
    N0_SKATER,
    RECENT_BONUS,
    SV_PCT_SHOTS,
    Window,
    blend,
    percentile,
    rookie_rates,
    xg_adjusted_goals,
)
from hockey.scoring.engine import ScoreBreakdown
from hockey.scoring.rules import build_rules
from hockey.sources.moneypuck import MoneyPuckShapeError, parse_goalies, parse_skaters
from tests.conftest import FIXTURES

RULES = build_rules(
    {
        "skater": {"G": 2, "A": 1.5, "SOG": 0.1, "PPP": 0.5},
        "goalie": {"W": 2, "GA": -1, "SV": 0.155, "SHO": 2},
    },
    source="t",
)
PRIOR = {"g": 0.3, "a": 0.4, "pts": 0.7, "sog": 2.5, "ppp": 0.2, "toi_min": 18.0}


def fake_prior(rates, proj_gp=80.0):
    return Projection(1, "F", 240, 200.0, 0.1, rates, proj_gp, 0.0, 0.0, ScoreBreakdown(0.0, {}))


# ------------------------------------------------------------------ MoneyPuck


def test_parse_moneypuck_skaters_uses_all_and_5on4_rows():
    lines = {
        ln.nhl_id: ln for ln in parse_skaters((FIXTURES / "moneypuck/2025_skaters.csv").read_text(), 20252026)
    }
    mcd = lines[8478402]
    assert mcd.gp == 80 and mcd.data["ixg"] == 38.5 and mcd.data["g"] == 48
    assert mcd.data["toi_min"] == pytest.approx(1320 * 80 / 60)
    assert mcd.data["pp_toi_min"] == pytest.approx(80 * 120 / 60)
    assert lines[8480800].pos_group == "D"


def test_parse_moneypuck_goalies():
    g = {
        ln.nhl_id: ln for ln in parse_goalies((FIXTURES / "moneypuck/2025_goalies.csv").read_text(), 20252026)
    }
    saros = g[8477424]
    assert saros.pos_group == "G" and saros.data["ga"] == 170 and saros.data["sa"] == 1620


def test_moneypuck_missing_columns_fail_loudly():
    with pytest.raises(MoneyPuckShapeError, match="I_F_xGoals"):
        parse_skaters("playerId,situation,games_played,icetime\n1,all,1,60\n", 20252026)


# ------------------------------------------------------------------ blend


def test_prior_only_when_no_games():
    p = blend(RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, prior_share=80 / 82)
    assert p.rates["g"] == pytest.approx(0.3) and p.rates["sog"] == pytest.approx(2.5)
    assert p.ros_gp == pytest.approx(80) and p.fp_per_gp == pytest.approx(0.6 + 0.6 + 0.25 + 0.1)


def test_stable_stats_move_faster_than_goals():
    season = Window(10, {"g": 8.0, "a": 4.0, "pts": 12.0, "sog": 40.0, "ppp": 2.0, "toi_min": 180.0})
    p = blend(RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, season=season, prior_share=0.97)
    # SOG: n0 = 10, so 10 games = half prior, half season.
    assert p.prior_weight["sog"] == pytest.approx(0.5)
    assert p.rates["sog"] == pytest.approx((10 * 2.5 + 40) / 20)
    # Goals: n0 = 50, so the 0.8 G/GP start barely moves the 0.3 prior.
    assert p.prior_weight["g"] == pytest.approx(50 / 60)
    assert 0.3 < p.rates["g"] < 0.42


def test_recent_games_count_extra():
    season = Window(20, {"g": 4.0, "sog": 50.0, "toi_min": 360.0})
    hot = Window(6, {"g": 4.0, "sog": 20.0, "toi_min": 108.0})
    base = blend(RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, season=season, prior_share=1)
    recent = blend(
        RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, season=season, last14=hot, prior_share=1
    )
    games = 20 + RECENT_BONUS["last14"] * 6
    assert recent.rates["g"] == pytest.approx((N0_SKATER["g"] * 0.3 + 4 + 0.5 * 4) / (N0_SKATER["g"] + games))
    assert recent.rates["g"] > base.rates["g"]


def test_xg_tempers_a_shooting_heater():
    season = Window(15, {"g": 13.0, "a": 7.0, "pts": 20.0, "sog": 45.0, "toi_min": 270.0})
    lucky = blend(
        RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, season=season, season_ixg=6.5, prior_share=1
    )
    naive = blend(RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, season=season, prior_share=1)
    assert lucky.rates["g"] < naive.rates["g"]
    assert any("running hot" in n for n in lucky.notes)
    assert xg_adjusted_goals(13, 6.5) == pytest.approx(13 - 0.4 * 6.5)


def test_role_change_scales_the_prior():
    more_ice = Window(10, {"toi_min": 10 * 21.0, "g": 3.0, "sog": 25.0})
    p = blend(
        RULES,
        "F",
        prior=fake_prior(PRIOR),
        prior_rates=PRIOR,
        season=more_ice,
        prior_share=1,
        pp_toi_prior=2.0,
        pp_toi_season=4.0,
    )
    toi_post = (5 * 18 + 210) / 15
    factor = toi_post / 18
    assert p.prior_rates["g"] == pytest.approx(0.3 * factor)
    pp_post = (10 * 2.0 + 4.0 * 10) / 20
    assert p.prior_rates["ppp"] == pytest.approx(0.2 * pp_post / 2.0)
    assert any("ice time" in n for n in p.notes) and any("power-play" in n for n in p.notes)


def test_goalie_save_pct_regressed_by_shots_and_consistent():
    prior = {
        "sa": 28.0,
        "sv": 28.0 * 0.905,
        "ga": 28.0 * 0.095,
        "w": 0.5,
        "so": 0.05,
        "gs": 1.0,
        "toi_min": 59,
    }
    season = Window(
        12, {"sa": 344.0, "sv": 300.0, "ga": 44.0, "w": 4.0, "so": 0.0, "gs": 12.0, "toi_min": 700}
    )
    p = blend(RULES, "G", prior=None, prior_rates=prior, season=season, prior_share=0.7)
    expected = (SV_PCT_SHOTS * 0.905 + 300) / (SV_PCT_SHOTS + 344)
    assert p.rates["sv_pct"] == pytest.approx(expected)
    assert 300 / 344 < p.rates["sv_pct"] < 0.905
    assert p.rates["sv"] + p.rates["ga"] == pytest.approx(p.rates["sa"])


def test_games_share_and_remaining():
    season = Window(10, {"g": 2.0, "toi_min": 180.0})
    p = blend(
        RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, season=season, team_gp=15, prior_share=1.0
    )
    share = (20 * 1.0 + 10) / (20 + 15)
    assert p.games_share == pytest.approx(share) and p.ros_gp == pytest.approx(share * 67)


def test_no_games_while_team_plays_lowers_share():
    p = blend(RULES, "F", prior=fake_prior(PRIOR), prior_rates=PRIOR, team_gp=15, prior_share=1.0)
    assert p.games_share == pytest.approx(20 / 35) and "0 GP" in p.notes[-1]
    assert p.rates["g"] == pytest.approx(0.3)  # the rate itself is untouched


def test_rookie_helpers():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3 and percentile([], 0.3) == 0.0
    assert percentile([0, 10], 0.3) == pytest.approx(3)
    assert rookie_rates({"g": 0.2, "a": 0.4}, mean_fp=2.0, target_fp=1.0) == {"g": 0.1, "a": 0.2}
