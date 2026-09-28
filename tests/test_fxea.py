import pytest

from hockey.sources import fantrax_fxea as fx
from tests.conftest import FakeHttp, load


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": "This league is not public"}, "This league is not public"),
        ({"pageError": {"code": "WARNING_NOT_LOGGED_IN", "title": "Not logged in"}}, "Not logged in"),
        ({"success": False, "message": "bad league"}, "bad league"),
        ({"rosters": {}}, None),
        ([1, 2], None),
    ],
)
def test_body_error(body, expected):
    assert fx.body_error(body) == expected


def test_client_raises_on_200_error_body():
    http = FakeHttp({"getLeagueInfo": load("fxea_error.json")})
    with pytest.raises(fx.FantraxError, match="not public"):
        fx.FxeaClient(http).league_info("testleague")


def test_parse_player_ids():
    players = {p.fantrax_id: p for p in fx.parse_player_ids(load("fxea_getPlayerIds.json"))}
    aho = players["04aho"]
    assert aho.name == "Sebastian Aho" and aho.nhl_team == "CAR" and aho.pos_group == "F"
    assert players["05aho"].pos_group == "D"
    assert players["06sla"].nhl_team == "MTL"  # Fantrax "MON"
    assert players["05jhu"].nhl_team == "NJD"  # Fantrax "NJ"
    assert players["07pro"].nhl_team is None  # "(N/A)"
    assert players["04dem"].pos_group == "G"


def test_parse_player_ids_list_shape():
    players = fx.parse_player_ids([{"id": "x1", "name": "Doe, John", "team": "TOR", "position": "D"}])
    assert players[0].fantrax_id == "x1" and players[0].name == "John Doe"


def test_parse_player_ids_garbage_raises():
    with pytest.raises(fx.FantraxShapeError):
        fx.parse_player_ids({"a": {"foo": 1}})


def test_parse_league_info():
    info = load("fxea_getLeagueInfo.json")
    teams = fx.parse_teams(info)
    assert teams["t01"]["name"] == "The Blue Blazers" and len(teams) == 3
    scoring = fx.parse_scoring(info)
    assert scoring["skater"]["G"] == 3 and scoring["skater"]["BkS"] == 0.5
    assert scoring["goalie"] == {"W": 4, "GA": -2, "SV": 0.2, "SO": 3}
    assert "G" not in scoring.get("goalie", {})  # "Goals" category must not be read as a goalie group


def test_parse_scoring_absent():
    assert fx.parse_scoring({"teamInfo": {}}) is None


def test_parse_scoring_keyed_by_code():
    info = {"scoring": {"skaters": {"G": {"points": 2}, "A": {"points": 1}}, "goalies": {"W": {"points": 5}}}}
    assert fx.parse_scoring(info) == {"skater": {"G": 2, "A": 1}, "goalie": {"W": 5}}


def test_parse_rosters():
    rows = fx.parse_rosters(load("fxea_getTeamRosters.json"))
    mine = [r for r in rows if r.team_id == "t01"]
    assert len(mine) == 7 and mine[0].team_name == "The Blue Blazers"
    assert {r.status for r in mine} == {"ACTIVE", "MINORS"}


def test_parse_rosters_bad_shape():
    with pytest.raises(fx.FantraxShapeError):
        fx.parse_rosters({"teams": []})
