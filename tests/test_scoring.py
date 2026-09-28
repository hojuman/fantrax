import pytest

from hockey.scoring.engine import MissingStatError, score
from hockey.scoring.rules import (
    ScoringConfigError,
    ScoringRules,
    build_rules,
    canonical_stat,
    rules_from_league_yaml,
)

RAW = {
    "skater": {"G": 3, "A": 2, "+/-": 0.5, "PPP": 1, "SOG": 0.4, "Hit": 0.25, "BkS": 0.5},
    "goalie": {"W": 4, "GA": -2, "SV": 0.2, "SO": 3},
}


@pytest.fixture
def rules():
    return build_rules(RAW, source="test")


def test_code_aliases():
    assert canonical_stat("+/-", "skater") == "pm"
    assert canonical_stat("BkS", "skater") == "blk"
    assert canonical_stat("Hits", "skater") == "hit"
    assert canonical_stat("PPPts", "skater") == "ppp"
    assert canonical_stat("SHO", "goalie") == "so"
    assert canonical_stat("G", "goalie") == "g"


def test_skater_score_and_breakdown(rules):
    line = {"g": 48, "a": 90, "pm": 20, "ppp": 45, "sog": 320, "hit": 40, "blk": 30}
    bd = score(rules, "F", line)
    assert bd.total == pytest.approx(144 + 180 + 10 + 45 + 128 + 10 + 15)
    assert sum(bd.parts.values()) == pytest.approx(bd.total)
    assert bd.top(1) == [("a", 180)]


def test_goalie_uses_goalie_weights(rules):
    bd = score(rules, "G", {"w": 26, "ga": 170, "sv": 1450, "so": 2})
    assert bd.total == pytest.approx(104 - 340 + 290 + 6)
    assert set(bd.parts) == {"w", "ga", "sv", "so"}


def test_defense_override():
    r = build_rules({"skater": {"G": 3, "A": 2}, "D": {"G": 1}}, source="t")
    assert score(r, "D", {"g": 10, "a": 10}).total == pytest.approx(10 * 1 + 20)  # D weight replaces skater G
    assert score(r, "F", {"g": 10, "a": 10}).total == pytest.approx(50)


def test_missing_stat_is_loud(rules):
    with pytest.raises(MissingStatError, match="blk"):
        score(rules, "F", {"g": 1, "a": 1, "pm": 0, "ppp": 0, "sog": 1, "hit": 0})
    assert score(rules, "F", {"g": 1}, strict=False).total == 3


def test_unknown_code_raises_with_all_problems():
    with pytest.raises(ScoringConfigError) as e:
        build_rules({"skater": {"G": 1, "Zamboni": 2, "Dangles": 1}}, source="t")
    assert "Zamboni" in str(e.value) and "Dangles" in str(e.value)


def test_unknown_zero_weight_code_is_skipped():
    r = build_rules({"skater": {"G": 1, "Zamboni": 0}}, source="t")
    assert r.weights == {"skater": {"g": 1.0}}


def test_rate_stat_refused():
    with pytest.raises(ScoringConfigError, match="Rate stat"):
        build_rules({"goalie": {"GAA": -1}}, source="t")


def test_code_aliases_and_ignore():
    r = build_rules(
        {"skater": {"Dangles": 1, "FW": 0.1, "Weird": 5}},
        source="t",
        code_aliases={"Dangles": "g"},
        ignore_codes=["Weird"],
    )
    assert r.weights["skater"] == {"g": 1.0, "fow": 0.1}


def test_stat_not_provided_by_source():
    with pytest.raises(ScoringConfigError, match="doesn't provide"):
        build_rules({"goalie": {"Dangles": 1}}, source="t", code_aliases={"Dangles": "hit"})


def test_rules_json_roundtrip(rules):
    assert ScoringRules.from_json(rules.to_json()).weights == rules.weights


def test_league_yaml_empty_scoring_is_none():
    assert rules_from_league_yaml({"scoring": {"skater": {}, "goalie": {}}}) is None
    assert rules_from_league_yaml({"scoring": {"skater": {"G": 2}}}).weights == {"skater": {"g": 2.0}}
