"""Phase 5: trade analyzer."""

import pytest
from typer.testing import CliRunner

from hockey import cli, db
from hockey.keepers import KeeperEntry
from hockey.sync import load_rules
from hockey.trade.analyze import Pick, TradeError, analyze, at_rank, pick_value, replacement_levels
from hockey.valuation import Valuer
from tests.test_sync_csv import full_sync

MINE, T2, T3 = "tbb0000000000000", "team020000000000", "team030000000000"
ROSTER = {"active": {"C": 2, "LW": 2, "RW": 2, "D": 4, "G": 2}, "reserve": 6, "effective_max": 26}


@pytest.fixture
def league(conn, settings):
    full_sync(conn, settings)
    v = Valuer(conn, load_rules(conn), settings.league)
    return v, v.league_players()


def rows(v, team, *ids):
    roster = v.team_roster(team)
    return [r for r in roster if r.fantrax_id in ids]


def run(v, pool, give, get, their=T3, their_name="Team 03", roster=ROSTER, **kw):
    return analyze(
        v.team_roster(MINE),
        v.team_roster(their),
        give,
        get,
        pool,
        my_name="The Blue Blazers",
        their_name=their_name,
        roster_cfg=roster,
        **kw,
    )


def test_goalie_for_a_depth_defenceman_fits_both_ways(league):
    v, pool = league
    r = run(v, pool, rows(v, MINE, "060v8"), rows(v, T3, "04dem"))
    # You have an empty G slot, so Demko starts; Team 03 loses its only goalie.
    assert r.me.lineup_change > 0 > r.them.lineup_change
    assert r.me.lineup_change == pytest.approx(
        -r.them.lineup_change + (r.me.lineup_change + r.them.lineup_change)
    )
    assert r.me.score > 0 > r.them.score and "Good for you, bad for them" in r.verdict


def test_bench_player_adds_no_lineup_value(league):
    v, pool = league
    # Getting O'Reilly (C) when your two C slots already have Aho and Stutzle: he'd sit.
    r = run(v, pool, rows(v, MINE, "060v8"), rows(v, T3, "02ore"))
    lost_d = next(p for p in r.me.sends).ros
    assert r.me.lineup_change == pytest.approx(-lost_d, abs=1e-6)


def test_scores_combine_lineup_and_future(league):
    v, pool = league
    r = run(
        v,
        pool,
        rows(v, MINE, "05stu"),
        rows(v, T2, "04abc"),
        their=T2,
        their_name="Team 02",
        keeper_weight=0.5,
    )
    for side in (r.me, r.them):
        assert side.score == pytest.approx(side.lineup_change + 0.5 * (side.keeper_change + side.pick_change))
    assert r.me.keeper_change == pytest.approx(-r.them.keeper_change)


def test_franchise_tag_and_keeper_clock_notes(league):
    v, pool = league
    keepers = {"05stu": KeeperEntry("05stu", "Tim Stutzle", times_kept=2, franchise_tag=True)}
    r = run(
        v, pool, rows(v, MINE, "05stu"), rows(v, T2, "04abc"), their=T2, their_name="Team 02", keepers=keepers
    )
    notes = r.me.sends[0].notes
    assert any("franchise-tagged" in n for n in notes) and any("clock resets" in n for n in notes)


def test_roster_overflow_when_receiving_more_players(league):
    v, pool = league
    full = {**ROSTER, "effective_max": 9}  # your 9-player roster is full
    r = run(
        v,
        pool,
        rows(v, MINE, "060v8"),
        rows(v, T2, "04abc", "03el6"),
        their=T2,
        their_name="Team 02",
        roster=full,
    )
    assert r.me.roster_overflow == 1 and any("would have to drop 1 player" in n for n in r.notes)


def test_validation_errors(league):
    v, pool = league
    with pytest.raises(TradeError, match="isn't on your roster"):
        run(v, pool, rows(v, T3, "02ore"), rows(v, T3, "04dem"))
    with pytest.raises(TradeError, match="something going each way"):
        run(v, pool, [], rows(v, T3, "04dem"))
    with pytest.raises(TradeError, match="2027:2"):
        Pick.parse("round two")


def test_picks_only_side(league):
    v, pool = league
    r = run(v, pool, [], rows(v, T3, "04dem"), give_picks=[Pick(2027, 1)])
    assert Pick(2027, 1) in r.pick_values and r.me.picks_sent == [Pick(2027, 1)]
    assert any("mid-round" in n for n in r.notes)


def test_pick_value_declines_by_round_and_hits_zero():
    values = [300 - i for i in range(300)]  # the 1st best is 300, the 300th is 1
    firsts = [pick_value(Pick(2027, rnd), values, teams=10, keepers_per_team=10) for rnd in range(1, 10)]
    assert firsts == sorted(firsts, reverse=True) and firsts[0] > 0
    assert firsts[0] == pytest.approx(at_rank(values, 100 + 5) - at_rank(values, 180))
    assert firsts[-1] == 0  # round 9 is past the waiver line


def test_replacement_levels_use_league_starter_counts(league):
    v, pool = league
    levels = replacement_levels(pool, teams=1, slots={"C": 2, "G": 1})
    cs = sorted(
        (r.projection.ros_fp for r in pool if r.projection and "C" in (r.positions or "").split(",")),
        reverse=True,
    )
    assert levels["C"] == pytest.approx(cs[2])  # first C past the 2 starters


@pytest.fixture
def cli_db(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("HOCKEY_DB", str(tmp_path / "t.db"))
    c = db.connect(tmp_path / "t.db")
    full_sync(c, settings)
    c.close()
    return CliRunner()


def test_cli_trade(cli_db):
    out = cli_db.invoke(cli.app, ["trade", "Elias Pettersson", "Demko"], env={"COLUMNS": "200"})
    assert out.exit_code == 0, out.output
    for text in (
        "Trade: The Blue Blazers sends Elias Pettersson",
        "Thatcher Demko",
        "Starting lineup",
        "Keeper value",
        "never sends or accepts trade offers",
        "MoneyPuck.com",
    ):
        assert text in out.output, text


def test_cli_trade_errors(cli_db):
    amb = cli_db.invoke(cli.app, ["trade", "Pettersson", "Demko"], env={"COLUMNS": "200"})
    assert amb.exit_code == 1 and "ambiguous" in amb.output and "060v8" in amb.output
    two = cli_db.invoke(cli.app, ["trade", "Stutzle", "McDavid, O'Reilly"], env={"COLUMNS": "200"})
    assert two.exit_code == 1 and "one team" in two.output
    picks_only = cli_db.invoke(
        cli.app, ["trade", "-", "Demko", "--give-pick", "2027:2"], env={"COLUMNS": "200"}
    )
    assert picks_only.exit_code == 0 and "2027 round 2" in picks_only.output
    nobody = cli_db.invoke(cli.app, ["trade", "Stutzle", "-", "--get-pick", "2027:1"])
    assert nobody.exit_code == 1 and "--partner" in nobody.output
    ok = cli_db.invoke(
        cli.app, ["trade", "Stutzle", "-", "--get-pick", "2027:1", "--partner", "T02"], env={"COLUMNS": "200"}
    )
    assert ok.exit_code == 0, ok.output


def test_next_season_value_is_aged(league):
    from hockey.trade.analyze import next_season_value

    v, pool = league
    mcd = next(r for r in pool if r.fantrax_id == "04abc")
    base = next_season_value(mcd)
    assert next_season_value(mcd, 24.0) == pytest.approx(base * 1.03)  # still improving
    assert next_season_value(mcd, 33.0) < base  # declining
    r = run(
        v,
        pool,
        rows(v, MINE, "05stu"),
        rows(v, T2, "04abc"),
        their=T2,
        their_name="Team 02",
        ages={8478402: 36.0},
    )
    assert r.me.receives[0].next_season == pytest.approx(base * 0.88)
