"""Phase 2 end to end: in-season sync on 2026-11-15, projections, and the rank/player commands."""

from datetime import date

import pytest
from typer.testing import CliRunner

from hockey import cli, db
from hockey.sync import load_rules
from hockey.valuation import Valuer
from tests.conftest import FakeHttp
from tests.test_sync_csv import full_sync

NOV15 = date(2026, 11, 15)


@pytest.fixture
def live(conn, settings):
    report = full_sync(conn, settings, FakeHttp(live=True), today=NOV15)
    return conn, settings, report


def test_in_season_data_is_stored(live):
    conn, settings, report = live
    assert report.problems == []
    assert report.counts["NHL last30 lines"] == 4 and report.counts["NHL last14 lines"] == 2
    assert conn.execute("SELECT gp FROM nhl_team_games WHERE team='CAR'").fetchone()[0] == 15
    assert conn.execute("SELECT COUNT(*) FROM mp_season WHERE season=20262027").fetchone()[0] > 0


def test_hot_start_is_tempered_and_explained(live):
    conn, settings, _ = live
    v = Valuer(conn, load_rules(conn), settings.league)
    aho = v.project(8478427)
    assert aho.method == "blend" and aho.season.gp == 15 and aho.last14.gp == 6
    raw_season_g = 13 / 15
    assert aho.prior_rates["g"] < aho.rates["g"] < raw_season_g  # moved up, but nowhere near 0.87 G/GP
    assert any("running hot" in n for n in aho.notes)
    assert any("power-play" in n for n in aho.notes)  # PP1 promotion showed up in the prior
    assert aho.uses_moneypuck
    assert 0 < aho.ros_gp <= 67  # 82 - 15 team games


def test_goalie_struggles_pull_projection_down(live):
    conn, settings, _ = live
    v = Valuer(conn, load_rules(conn), settings.league)
    saros = v.project(8477424)
    assert saros.rates["sv_pct"] < 1450 / 1620  # below last season's .895, not all the way to .872
    assert saros.rates["sv_pct"] > 300 / 344


def test_rookies(live, conn, settings):
    v = Valuer(conn, load_rules(conn), settings.league)
    newby = v.project(8485555)
    assert newby.method == "blend (rookie prior)" and newby.season.gp == 12 and newby.ros_gp > 0


def test_preseason_rookie_default(conn, settings):
    full_sync(conn, settings)  # 2026-09-28: no games yet
    v = Valuer(conn, load_rules(conn), settings.league)
    newby = v.project(8485555)
    from hockey.projection.inseason import ON_ROSTER_SHARE

    assert newby.method == "rookie default"
    assert newby.ros_gp == pytest.approx(ON_ROSTER_SHARE[newby.pos_group] * 82)  # on an NHL roster
    assert "no NHL games" in newby.notes[0]
    mcdavid = v.project(8478402)
    assert 0 < newby.fp_per_gp < mcdavid.fp_per_gp
    prospect = next(r for r in v.team_roster("tbb0000000000000") if r.fantrax_id == "07pro")
    assert prospect.projection.method == "rookie default" and prospect.projection.ros_gp == 0


def test_mcdavid_xg_note_in_preseason(conn, settings):
    full_sync(conn, settings)
    v = Valuer(conn, load_rules(conn), settings.league)
    assert any("48 G on 38.5 ixG" in n for n in v.project(8478402).notes)


@pytest.fixture
def cli_db(tmp_path, monkeypatch, settings):
    path = tmp_path / "cli.db"
    monkeypatch.setenv("HOCKEY_DB", str(path))
    monkeypatch.setenv("FANTRAX_LEAGUE_ID", "testleague")
    c = db.connect(path)
    full_sync(c, settings, FakeHttp(live=True), today=NOV15)
    c.close()
    return CliRunner()


def test_cli_rank_available(cli_db):
    out = cli_db.invoke(cli.app, ["rank", "--available", "--pos", "C"], env={"COLUMNS": "200"})
    assert out.exit_code == 0, out.output
    assert "Rookie Newby" in out.output and "Jack Hughes" in out.output
    assert "Connor McDavid" not in out.output  # rostered
    assert "MoneyPuck.com" in out.output  # credit line


def test_cli_rank_owner_and_sort(cli_db):
    out = cli_db.invoke(cli.app, ["rank", "--owner", "TBB", "--sort", "ros"], env={"COLUMNS": "200"})
    assert out.exit_code == 0, out.output
    assert "Sebastian Aho" in out.output and "Connor McDavid" not in out.output
    bad = cli_db.invoke(cli.app, ["rank", "--pos", "X"])
    assert bad.exit_code == 2


def test_cli_player_detail_and_disambiguation(cli_db):
    many = cli_db.invoke(cli.app, ["player", "Sebastian Aho"], env={"COLUMNS": "200"})
    assert many.exit_code == 1 and "03rmx" in many.output and "03el6" in many.output
    one = cli_db.invoke(cli.app, ["player", "03rmx"], env={"COLUMNS": "200"})
    assert one.exit_code == 0, one.output
    for text in (
        "Per-game rates",
        "Last 14 (6)",
        "Prior wt",
        "running hot",
        "Rest-of-season points",
        "MoneyPuck.com",
    ):
        assert text in one.output, text


def test_cli_roster_v2(cli_db):
    out = cli_db.invoke(cli.app, ["roster"], env={"COLUMNS": "220"})
    assert out.exit_code == 0, out.output
    assert "ROS FP" in out.output and "rookie est." in out.output and "MoneyPuck.com" in out.output


def test_moneypuck_placeholder_for_current_season_is_only_a_note(conn, settings):

    class Placeholder(FakeHttp):
        def get(self, url, params=None, **kw):
            from hockey.http import Response

            if "seasonSummary/2026/" in url:
                return Response(url, 200, "<html>coming soon</html>", False)
            return super().get(url, params, **kw)

    report = full_sync(conn, settings, Placeholder())
    assert report.problems == [] and any("no usable 2026-27" in n for n in report.notes)
    assert report.counts["MoneyPuck lines"] > 0
