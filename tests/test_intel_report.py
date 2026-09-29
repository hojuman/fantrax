"""Phase 7: league intel and the daily markdown report."""

from datetime import UTC, date, datetime

import pytest
from typer.testing import CliRunner

from hockey import cli, db
from hockey.intel.league import Asset, build_intel, opponent_for, profile, rank_league, swaps_with
from hockey.report.daily import render, write
from hockey.sources.nhl import NhlClient
from hockey.sync import load_rules
from hockey.valuation import Valuer
from tests.conftest import FakeHttp, load
from tests.test_lineup import P2
from tests.test_sync_csv import full_sync

MINE = "tbb0000000000000"
SMALL = {"C": 1, "G": 1}


def a(i, pos, ros):
    return Asset(i, i, (pos,), ros)


def test_mutual_swap_is_found():
    mine = [a("c1", "C", 100), a("c2", "C", 90)]  # your backup C sits
    theirs = [a("g1", "G", 80), a("g2", "G", 70)]  # their backup G sits
    swaps = swaps_with(mine, theirs, "Them", SMALL)
    top = swaps[0]
    # Your backup C for their starting G: you +80; they +90 for the C, -10 as their backup G steps in.
    assert (top.give.id, top.get.id) == ("c2", "g1")
    assert top.my_gain == pytest.approx(80) and top.their_gain == pytest.approx(80)
    assert {s.get.id for s in swaps} == {"g1", "g2"}  # the backup-for-backup idea is still listed


def test_one_sided_swaps_are_not_suggested():
    mine = [a("c1", "C", 100), a("g1", "G", 50)]
    theirs = [a("c9", "C", 120), a("g9", "G", 60)]  # every swap hurts one side
    assert swaps_with(mine, theirs, "Them", SMALL) == []


def test_profiles_and_ranks():
    ps = [
        profile(str(i), f"T{i}", [a(f"c{i}", "C", 10 * i), a(f"g{i}", "G", 100 - 10 * i)], SMALL)
        for i in range(1, 11)
    ]
    rank_league(ps, SMALL)
    best_c = next(p for p in ps if p.name == "T10")
    assert best_c.ranks["C"] == 1 and "C" in best_c.strengths and "G" in best_c.weaknesses
    assert sum("C" in p.strengths for p in ps) == 3 and sum("C" in p.weaknesses for p in ps) == 3


def test_opponent_lookup():
    matchups = load("fxea_getLeagueInfo.json")["matchups"]
    opp1 = opponent_for(matchups, 1, MINE)
    assert opp1 and opp1 != MINE and opponent_for(matchups, 2, MINE) != opp1
    assert opponent_for(matchups, 99, MINE) is None


@pytest.fixture
def league(conn, settings):
    full_sync(conn, settings)
    v = Valuer(conn, load_rules(conn), settings.league)
    teams = conn.execute("SELECT DISTINCT team_id, name FROM fantasy_team").fetchall()
    return {t["team_id"]: (t["name"], v.team_roster(t["team_id"])) for t in teams}


def test_build_intel_on_fixture_league(league):
    li = build_intel(league, MINE, opponent_id="team030000000000")
    assert li.me.name == "The Blue Blazers" and li.me.overall_rank == 1
    assert li.opponent.name == "Team 03"
    team03, swaps = next((t, s) for t, s in li.partners if t.name == "Team 03")
    assert swaps[0].get.name == "Thatcher Demko" and swaps[0].my_gain > 0 and swaps[0].their_gain > 0
    assert len({s.get.id for s in swaps}) == len(swaps)  # one idea per player you'd receive


def test_render_markdown(league, tmp_path):
    li = build_intel(league, MINE, opponent_id="team030000000000")
    md = render("The Blue Blazers", datetime(2026, 10, 3, 12, tzinfo=UTC), league=li, errors=["test warning"])
    assert (
        md.startswith("# The Blue Blazers: daily report") and "## League" in md and "> ⚠ test warning" in md
    )
    assert "This week's opponent: **Team 03**" in md and 'hockey trade "' in md and "MoneyPuck.com" in md
    path = write(md, tmp_path / "reports", datetime(2026, 10, 3, 12, tzinfo=UTC))
    assert path.name == "2026-10-03.md" and path.read_text() == md


@pytest.fixture
def cli_db(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("HOCKEY_DB", str(tmp_path / "r.db"))
    c = db.connect(tmp_path / "r.db")
    full_sync(c, settings)
    c.close()
    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    monkeypatch.setattr(NhlClient, "schedule", lambda self, s, e: games)
    return CliRunner()


def test_cli_intel(cli_db):
    out = cli_db.invoke(cli.app, ["intel"], env={"COLUMNS": "200"})
    assert out.exit_code == 0, out.output
    for text in (
        "starting-lineup strength",
        "The Blue Blazers",
        "Trade partners",
        "Thatcher Demko",
        "MoneyPuck.com",
    ):
        assert text in out.output, text


def test_cli_report(cli_db, tmp_path):
    ok = cli_db.invoke(
        cli.app,
        ["report", "--out-dir", str(tmp_path / "rep"), "--out", "Quinn Hughes"],
        env={"COLUMNS": "200"},
    )
    assert ok.exit_code == 0, ok.output
    files = list((tmp_path / "rep").glob("*.md"))
    assert len(files) == 1
    md = files[0].read_text()
    for text in (
        "## Lineup: period",
        "Move Quinn Hughes to Reserve",
        "## Waivers",
        "Juraj Slafkovsky",
        "## League",
        "Recommendations only",
    ):
        assert text in md, text
