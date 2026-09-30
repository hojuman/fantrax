"""Phase 6: age curve, keeper value, the rules, and the exact keeper-set search."""

import itertools
import random
from datetime import date

import pytest
from typer.testing import CliRunner

from hockey import cli, db
from hockey.keeper.aging import age_factor, age_on, season_start
from hockey.keeper.plan import (
    DISCOUNT,
    Rules,
    build_candidate,
    choose,
    discounted,
    drop_costs,
    plan,
    yearly_values,
)
from hockey.keepers import KeeperEntry, load_keepers
from hockey.projection.inseason import blend
from hockey.scoring.rules import build_rules
from hockey.sources.nhl import parse_landing
from hockey.valuation import RosterRow
from tests.conftest import FakeHttp, load
from tests.test_sync_csv import full_sync

RULES = Rules()
SCORING = build_rules({"skater": {"G": 2}, "goalie": {"W": 2}}, source="t")


def row(fid="x", name="Player X", pos="F", positions="C", fp_rate=1.0, share=1.0, ros_gp=60.0):
    proj = blend(SCORING, pos, prior=None, prior_rates={"g": fp_rate / 2}, prior_share=share)
    proj.ros_gp = ros_gp
    return RosterRow(fid, name, positions, pos, "CAR", "ACTIVE", None, 1, "exact", proj)


def cand(r=None, *, age=27.0, gp=500, entry=None, line=50.0):
    r = r or row()
    return build_candidate(
        r, age_next=age, career_gp=gp, known_career=True, entry=entry, keeper_line=line, rules=RULES
    )


# ------------------------------------------------------------------ aging + landing


def test_age_curve_shapes():
    assert age_factor(21, "F") > age_factor(25, "F") == 1.0 > age_factor(30, "F") > age_factor(35, "F")
    assert age_factor(28, "D") > age_factor(28, "F")  # D age a year later
    assert age_factor(30, "G") == 1.0 and age_factor(None, "F") == 1.0
    assert age_on("1997-01-13", season_start(2027)) == pytest.approx(30.7, abs=0.05)
    assert age_on(None, date(2027, 10, 1)) is None and age_on("garbage", date(2027, 10, 1)) is None


def test_parse_landing():
    pages = load("nhl_landing.json")
    mcd = parse_landing(pages["8478402"], 8478402)
    assert mcd.gp == 750 and mcd.birth_date == "1997-01-13"
    assert parse_landing(pages["8485555"], 8485555).gp == 0  # no NHL games: no careerTotals


def test_yearly_values_age_forward():
    young = yearly_values(100, age_next=22, pos_group="F")
    old = yearly_values(100, age_next=33, pos_group="F")
    assert young[0] > 100 and young == sorted(young)  # still improving
    assert old[0] < 100 and old == sorted(old, reverse=True)  # declining
    assert discounted([100, 100, 100], 2, line=40) == pytest.approx(60 + DISCOUNT * 60)


# ------------------------------------------------------------------ rules


def test_clock_limits_the_untagged_horizon():
    fresh = cand(entry=KeeperEntry("x", "X", times_kept=0))
    twice = cand(entry=KeeperEntry("x", "X", times_kept=2))
    maxed = cand(entry=KeeperEntry("x", "X", times_kept=3))
    assert fresh.regular_untagged > twice.regular_untagged > 0
    assert twice.regular_tagged == pytest.approx(fresh.regular_untagged)  # a tag restores the full horizon
    assert maxed.regular_untagged is None and maxed.regular_tagged is not None
    assert any("needs a franchise tag" in n for n in maxed.notes)


def test_removed_tag_cannot_come_back():
    c = cand(entry=KeeperEntry("x", "X", times_kept=3, tag_removed=True))
    assert c.regular_untagged is None and c.regular_tagged is None
    assert any("not allowed" in n for n in c.notes)


def test_untagging_a_maxed_player_is_not_an_option():
    c = cand(entry=KeeperEntry("x", "X", times_kept=3, franchise_tag=True))
    assert c.regular_untagged is None and c.regular_tagged is not None


def test_minors_eligibility_is_judged_at_keeper_time():
    stays = cand(row(ros_gp=40), gp=120)  # 160 at keeper time
    grads = cand(row(ros_gp=60), gp=150)  # 210: graduates
    assert stays.minors is not None and stays.regular_untagged is not None
    assert grads.minors is None and any("graduates from minors" in n for n in grads.notes)
    at_line = cand(row(ros_gp=0), gp=165)  # exactly 165 is still a minor (<=)
    assert at_line.minors is not None


def test_minors_clock_is_off():
    c = cand(row(ros_gp=10), gp=50, entry=KeeperEntry("x", "X", times_kept=3))
    assert c.regular_untagged is not None  # the clock hasn't started


# ------------------------------------------------------------------ the search


def brute(cands, rules):
    best = 0.0
    opts = []
    for c in cands:
        o = [("release", 0.0)]
        if c.minors is not None:
            o.append(("minors", c.minors))
        if c.regular_untagged is not None:
            o.append(("regular", c.regular_untagged))
        if c.regular_tagged is not None:
            o.append(("regular+tag", c.regular_tagged))
        opts.append(o)
    for combo in itertools.product(*opts):
        names = [n for n, _ in combo]
        reg = sum(n.startswith("regular") for n in names)
        if (
            reg <= rules.regular
            and names.count("regular+tag") <= rules.tags
            and names.count("minors") <= rules.minors
        ):
            best = max(best, sum(v for _, v in combo))
    return best


@pytest.mark.parametrize("seed", range(20))
def test_choice_is_optimal(seed):
    rng = random.Random(seed)
    rules = Rules(regular=3, minors=2, tags=1)
    cands = []
    for i in range(7):
        c = cand(
            row(f"p{i}", fp_rate=rng.uniform(0.5, 3), ros_gp=rng.choice([0, 30, 60])),
            gp=rng.choice([20, 150, 600]),
            age=rng.uniform(20, 35),
            entry=KeeperEntry(
                f"p{i}",
                "",
                times_kept=rng.choice([0, 2, 3]),
                franchise_tag=rng.random() < 0.2,
                tag_removed=rng.random() < 0.1,
            ),
        )
        cands.append(c)
    assert choose(cands, rules) == pytest.approx(brute(cands, rules), abs=1e-3)


def test_plan_warnings_for_tags():
    tagged_keeps_left = cand(
        row("a", "Tagged Guy"), entry=KeeperEntry("a", "", times_kept=0, franchise_tag=True)
    )
    weak_tagged = cand(row("b", "Weak Tagged", fp_rate=0.1), entry=KeeperEntry("b", "", franchise_tag=True))
    p = plan([tagged_keeps_left, weak_tagged], Rules(tags=0), keeper_line=50)
    assert tagged_keeps_left.choice == "regular"
    assert any("Moving the franchise tag off Tagged Guy is permanent" in w for w in p.warnings)
    assert any("Weak Tagged is franchise-tagged but not worth keeping" in w for w in p.warnings)


def test_load_keepers(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text(
        "players:\n  - {player: A, fantrax_id: '1', times_kept: 2, franchise_tag: true}\n  - {player: B}\n"
    )
    k = load_keepers(f)
    assert list(k) == ["1"] and k["1"].times_kept == 2 and k["1"].franchise_tag
    assert load_keepers(tmp_path / "missing.yaml") == {}


# ------------------------------------------------------------------ CLI


def test_cli_keepers(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("HOCKEY_DB", str(tmp_path / "k.db"))
    c = db.connect(tmp_path / "k.db")
    full_sync(c, settings)
    c.close()
    monkeypatch.setattr(cli, "HttpClient", lambda conn, refresh=False: FakeHttp())
    kf = tmp_path / "keepers.yaml"
    kf.write_text(
        "players:\n  - {player: Sebastian Aho, fantrax_id: '03rmx', times_kept: 3, franchise_tag: true}\n"
        "  - {player: JT Miller, fantrax_id: '03mil', times_kept: 3}\n"
    )
    out = CliRunner().invoke(cli.app, ["keepers", "--keepers-file", str(kf)], env={"COLUMNS": "220"})
    assert out.exit_code == 0, out.output
    for text in (
        "keeper plan",
        "Keep + tag",
        "Sebastian Aho",
        "Keep (minors)",
        "Elias Pettersson",
        "no NHL record",
        "franchise tags:",
        "MoneyPuck.com",
    ):
        assert text in out.output, text


# ------------------------------------------------------------------ drop costs + unknown prospects


def test_drop_costs_are_marginal_keeper_value():
    rules = Rules(regular=2, minors=1, tags=0)
    star = cand(row("s", fp_rate=2.0), line=10.0)
    good = cand(row("g", fp_rate=1.5), line=10.0)
    meh = cand(row("m", fp_rate=1.0), line=10.0)  # third-best regular: released with 2 slots
    prospect = cand(row("p", fp_rate=0.8), age=20.0, gp=9, line=10.0)  # minors-eligible
    cands = [star, good, meh, prospect]
    costs = drop_costs(cands, rules)
    total = choose([c for c in cands], rules)

    def without(x):
        return choose([cand(c.row, age=c.age, gp=c.career_gp, line=10.0) for c in cands if c is not x], rules)

    for c in cands:
        assert costs[c.row.fantrax_id] == pytest.approx(total - without(c))
    assert costs["m"] == pytest.approx(0.0)  # would be released anyway
    assert costs["s"] > costs["g"] > 0 and costs["p"] > 0
    # Losing the star only costs what the released third regular can't replace.
    assert costs["s"] == pytest.approx(star.regular_untagged - meh.regular_untagged)


def test_players_with_no_nhl_record_are_unknown_not_worthless():
    known = cand(row("k", fp_rate=2.0))
    ghost = build_candidate(
        row("u", name="Caleb Desnoyers"),
        age_next=None,
        career_gp=0,
        known_career=False,
        entry=None,
        keeper_line=50.0,
        rules=RULES,
    )
    p = plan([known, ghost], RULES, 50.0)
    assert ghost.unknown and ghost.choice == "unknown"
    assert any("Caleb Desnoyers" in w and "unknown, not zero" in w for w in p.warnings)
    costs = drop_costs([known, ghost], RULES)
    assert "u" not in costs  # callers treat a missing cost as unknown
