import json

from typer.testing import CliRunner

from hockey.scoring_check import check_csv
from hockey.sources.fantrax_csv import is_rostered_status, read_players_csv
from hockey.sync import (
    SyncReport,
    find_my_team,
    import_csv,
    load_rules,
    run_idmap,
    sync_fantrax,
    sync_moneypuck,
    sync_nhl,
)
from hockey.valuation import Valuer
from tests.conftest import FIXTURES, FakeHttp, load

TODAY = __import__("datetime").date(2026, 9, 28)


def full_sync(conn, settings, http=None, today=TODAY):
    http = http or FakeHttp()
    report = SyncReport()
    sync_fantrax(conn, http, settings, report)
    sync_nhl(conn, http, today, report)
    sync_moneypuck(conn, http, today, report)
    run_idmap(conn, report)
    return report


MINE = "tbb0000000000000"


def test_full_sync_and_my_roster(conn, settings):
    report = full_sync(conn, settings)
    assert report.problems == []
    assert any("max players: Fantrax says 28" in n for n in report.notes)
    rules = load_rules(conn)
    assert rules.source == "fantrax" and rules.weights["goalie"] == {
        "ga": -1.0,
        "sv": 0.155,
        "so": 2.0,
        "w": 2.0,
    }

    team = find_my_team(conn, settings)
    assert team["team_id"] == MINE and team["short_name"] == "TBB"
    rows = {r.fantrax_id: r for r in Valuer(conn, rules, settings.league).team_roster(MINE)}
    assert len(rows) == 9
    assert rows["03rmx"].nhl_id == 8478427  # the Carolina Aho, not the one now in Pittsburgh
    assert rows["060v8"].nhl_id == 8483678  # the defenceman Pettersson
    assert rows["03daz"].nhl_id == 8477969  # Marcus, not either Elias
    assert rows["03mar"].match_method == "alias"  # "Mitchell" vs "Mitch"
    assert rows["07pro"].nhl_id is None  # prospect: logged, no projection
    saros = rows["04sar"].projection
    assert saros and saros.pos_group == "G" and saros.ros_gp == 58 and saros.method == "prior only"
    assert rows["05qhu"].projection.fp_per_gp > rows["060v8"].projection.fp_per_gp

    mapped = dict(conn.execute("SELECT fantrax_id, nhl_id FROM player_map").fetchall())
    assert mapped["03el6"] == 8480222 and mapped["02ore"] == 8475158
    unmatched = dict(conn.execute("SELECT fantrax_id, reason FROM id_unmatched").fetchall())
    # The team-less "OReilly, Ryan" loses the conflict to the real one; the two Hugo Petterssons,
    # Karri Aho and the prospect have no NHL record at all.
    assert set(unmatched) == {"04qz4", "05uxb", "074qj", "05lun", "07pro"}
    assert unmatched["04qz4"].startswith("conflict")
    assert (
        conn.execute("SELECT COUNT(*) FROM meta WHERE key IN ('roster_periods', 'matchups')").fetchone()[0]
        == 2
    )


def test_second_run_is_sticky(conn, settings):
    full_sync(conn, settings)
    full_sync(conn, settings)
    methods = dict(conn.execute("SELECT fantrax_id, method FROM player_map").fetchall())
    assert methods["03rmx"] == "sticky" and methods["03mar"] == "sticky"
    assert "04qz4" not in methods  # the namesake still doesn't sneak in


def test_private_league_falls_back(conn, settings):
    http = FakeHttp({"getLeagueInfo": load("fxea_error.json"), "getTeamRosters": load("fxea_error.json")})
    settings.league = {**settings.league, "scoring": {"skater": {"G": 2, "A": 1}, "goalie": {"W": 2}}}
    report = full_sync(conn, settings, http)
    assert any("getLeagueInfo refused" in p for p in report.problems)
    assert any("import-csv" in p for p in report.problems)
    assert load_rules(conn).source == "league.yaml"

    counts = import_csv(conn, FIXTURES / "fantrax_players.csv")
    assert counts == {"rows": 5, "rostered": 4}
    settings.my_team_short = "TBB"
    team = find_my_team(conn, settings)
    assert team["team_id"] == "csv:TBB"
    run_idmap(conn, SyncReport())
    rows = Valuer(conn, load_rules(conn), settings.league).team_roster("csv:TBB")
    assert {r.name for r in rows} == {"Sebastian Aho", "Quinn Hughes", "Juuse Saros"}
    assert all(r.projection for r in rows)
    fa = conn.execute("SELECT pool_status FROM fantrax_player WHERE fantrax_id='05jhu'").fetchone()[0]
    assert fa == "FA"


def test_no_scoring_anywhere_is_a_problem(conn, settings):
    http = FakeHttp({"getLeagueInfo": load("fxea_error.json")})
    report = full_sync(conn, settings, http)
    assert any("No scoring weights" in p for p in report.problems)


def test_csv_reader():
    rows = {r.fantrax_id: r for r in read_players_csv(FIXTURES / "fantrax_players.csv")}
    assert rows["04abc"].name == "Connor McDavid" and rows["04abc"].fpts == 299.1
    assert rows["04abc"].stats["SOG"] == 320 and "FP/G" not in rows["04abc"].stats
    assert rows["05jhu"].nhl_team == "NJD" and rows["03rmx"].status == "TBB"
    assert is_rostered_status("TBB") and not is_rostered_status("FA") and not is_rostered_status("W (Tue)")


def test_validate_scoring_pass_and_fail(conn, settings):
    full_sync(conn, settings)
    rules = load_rules(conn)
    res = check_csv(FIXTURES / "fantrax_players.csv", rules)
    assert res.ok and res.checked == 5
    rules.weights["skater"]["sog"] = 0.5  # wrong weight -> every skater off
    res = check_csv(FIXTURES / "fantrax_players.csv", rules)
    assert not res.ok and res.within == 1 and res.worst


def test_cli_roster_smoke(tmp_path, monkeypatch, settings):
    from hockey import cli, db

    monkeypatch.setenv("HOCKEY_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("FANTRAX_LEAGUE_ID", "testleague")
    monkeypatch.setenv("MY_TEAM_NAME", "The Blue Blazers")
    conn = db.connect(tmp_path / "cli.db")
    full_sync(conn, settings)
    conn.close()
    result = CliRunner().invoke(cli.app, ["roster"])
    assert result.exit_code == 0, result.output
    assert "The Blue Blazers" in result.output and "Sebastian Aho" in result.output
    assert "No NHL record (rookie estimate, 0 games projected): Future Prospect" in result.output
    ids = CliRunner().invoke(cli.app, ["ids", "--unmatched"])
    assert "Future Prospect" in ids.output


def test_probe_sanitize():
    from hockey.probe import sanitize

    data = {
        "teams": {"t1": {"name": "The Blue Blazers", "ownerName": "Real Person", "email": "a@b.c"}},
        "players": {f"p{i}": {"name": f"Player {i}"} for i in range(40)} | {"zz": {"name": "Aho, Sebastian"}},
    }
    s = sanitize(data)
    assert s["teams"]["t1"]["ownerName"].startswith("anon-") and s["teams"]["t1"]["email"].startswith("anon-")
    assert s["teams"]["t1"]["name"] == "The Blue Blazers"
    assert len(s["players"]) == 26 and "zz" in s["players"]
    json.dumps(s)


def test_cli_sync_reports_fantrax_refusal(tmp_path, monkeypatch):
    from hockey import cli

    monkeypatch.setenv("HOCKEY_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("FANTRAX_LEAGUE_ID", "testleague")
    monkeypatch.setattr(
        cli, "HttpClient", lambda conn, refresh=False: FakeHttp({"getPlayerIds": load("fxea_error.json")})
    )
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 1 and "hockey probe" in result.output and "Traceback" not in result.output


def test_nhl_roster_failure_for_one_team_does_not_abort(conn, settings):
    from hockey.http import FetchError

    class Flaky(FakeHttp):
        def get(self, url, params=None, **kw):
            if "/roster/VAN/" in url:
                raise FetchError(url, None, "boom")
            return super().get(url, params, **kw)

    report = full_sync(conn, settings, Flaky())
    assert any("VAN" in n for n in report.notes)
    assert report.counts["NHL roster players"] > 0


def test_roster_urls_use_explicit_season(conn, settings, fake_http):
    full_sync(conn, settings, fake_http)
    roster_calls = [c for c in fake_http.calls if "/roster/" in c]
    assert len(roster_calls) == 32 and all(c.endswith("/20262027") for c in roster_calls)
