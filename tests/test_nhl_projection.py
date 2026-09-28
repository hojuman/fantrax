from datetime import date

import pytest

from hockey.projection.baseline import position_means, project_player
from hockey.scoring.rules import build_rules
from hockey.sources.nhl import SeasonLine, completed_seasons, current_season, goalie_lines, merge_skater_rows
from hockey.sources.teams import normalize_team, pos_group
from tests.conftest import load


def test_seasons():
    assert current_season(date(2026, 9, 28)) == 20262027
    assert current_season(date(2027, 3, 1)) == 20262027
    assert completed_seasons(date(2026, 9, 28)) == [20252026, 20242025, 20232024]


def test_team_and_position_helpers():
    assert normalize_team("TB") == "TBL" and normalize_team("(N/A)") is None and normalize_team("XYZ") is None
    assert (
        pos_group("C,LW") == "F" and pos_group("D") == "D" and pos_group("G") == "G" and pos_group("L") == "F"
    )


def test_merge_skater_rows_derives_stats():
    s = load("nhl_stats.json")["20252026"]
    lines = {
        ln.nhl_id: ln
        for ln in merge_skater_rows(
            s["skater/summary"], s["skater/realtime"], s["skater/faceoffwins"], 20252026
        )
    }
    mc = lines[8478402]
    assert mc.gp == 80 and mc.stats["ppa"] == 45 - 15 and mc.stats["hit"] == 40 and mc.stats["fow"] == 600
    assert mc.stats["toi_min"] == pytest.approx(1320 * 80 / 60)
    assert lines[8476468].team == "NYR"


def test_goalie_lines():
    g = goalie_lines(load("nhl_stats.json")["20252026"]["goalie/summary"], 20252026)
    saros = next(x for x in g if x.nhl_id == 8477424)
    assert saros.pos_group == "G" and saros.stats["sv"] == 1450 and saros.stats["gs"] == 57


RULES = build_rules({"skater": {"G": 3, "A": 2}}, source="t")


def line(pid, season, gp, g, a, pos="F"):
    return SeasonLine(pid, season, "", None, pos, None, gp, {"gp": gp, "g": g, "a": a})


def test_projection_weights_and_regression():
    seasons = [20252026, 20242025, 20232024]
    means = {"F": {"g": 0.2, "a": 0.3}}
    vet = project_player(
        [line(1, 20252026, 82, 41, 41), line(1, 20242025, 82, 41, 41), line(1, 20232024, 82, 41, 41)],
        seasons,
        means,
        RULES,
    )
    callup = project_player([line(2, 20252026, 5, 5, 0)], seasons, means, RULES)
    # Veteran: 0.5 G/GP, barely regressed. Call-up: 1.0 G/GP over 5 games, pulled hard to 0.2.
    # Weights normalize to 1 / 0.8 / 0.6, so k=25 means 25 real games of an average forward.
    assert vet.rates["g"] == pytest.approx((1 * 41 + 0.8 * 41 + 0.6 * 41 + 25 * 0.2) / (2.4 * 82 + 25))
    assert vet.shrink < 0.12 and callup.shrink > 0.8
    assert callup.rates["g"] == pytest.approx((5 + 25 * 0.2) / 30)
    assert vet.fp_per_gp == pytest.approx(3 * vet.rates["g"] + 2 * vet.rates["a"])
    assert vet.fp_season == pytest.approx(vet.fp_per_gp * 82)
    assert sum(vet.breakdown.parts.values()) == pytest.approx(vet.fp_per_gp)


def test_recent_season_weighs_more():
    seasons = [20252026, 20242025]
    means = {"F": {"g": 0.2, "a": 0.0}}
    rising = project_player(
        [line(1, 20252026, 82, 40, 0), line(1, 20242025, 82, 10, 0)], seasons, means, RULES
    )
    falling = project_player(
        [line(1, 20252026, 82, 10, 0), line(1, 20242025, 82, 40, 0)], seasons, means, RULES
    )
    assert rising.fp_per_gp > falling.fp_per_gp


def test_no_data_no_projection():
    assert project_player([], [20252026], {}, RULES) is None
    assert project_player([line(1, 20192020, 82, 1, 1)], [20252026], {}, RULES) is None


def test_gp_cap_and_means():
    lines = [line(1, 20252026, 82, 20, 20), line(2, 20252026, 10, 10, 10), line(3, 20252026, 60, 6, 0, "D")]
    means = position_means(lines, 20252026)
    assert means["F"]["g"] == pytest.approx(20 / 82)  # the 10-GP player is excluded from the mean
    g = project_player([line(9, 20252026, 70, 0, 0, "G")], [20252026], means, RULES)
    assert g.proj_gp == 65
