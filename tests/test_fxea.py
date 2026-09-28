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
    aho = players["03rmx"]
    assert aho.name == "Sebastian Aho" and aho.nhl_team == "CAR" and aho.pos_group == "F"
    assert players["03el6"].pos_group == "D" and players["03el6"].nhl_team == "PIT"
    assert players["04qz4"].name == "Ryan OReilly" and players["04qz4"].nhl_team is None  # "(N/A)"
    assert players["04dem"].pos_group == "G"
    assert players["03rmx"].extra == {"rotowireId": 4900, "statsIncId": 6777}


def test_parse_player_ids_fantasy_team_codes():
    data = {
        "x": {"fantraxId": "x", "name": "Slafkovsky, Juraj", "team": "MON", "position": "LW"},
        "y": {"fantraxId": "y", "name": "Hughes, Jack", "team": "NJ", "position": "C"},
    }
    p = {x.fantrax_id: x for x in fx.parse_player_ids(data)}
    assert p["x"].nhl_team == "MTL" and p["y"].nhl_team == "NJD"


def test_parse_player_ids_list_shape():
    players = fx.parse_player_ids([{"id": "x1", "name": "Doe, John", "team": "TOR", "position": "D"}])
    assert players[0].fantrax_id == "x1" and players[0].name == "John Doe"


def test_parse_player_ids_garbage_raises():
    with pytest.raises(fx.FantraxShapeError):
        fx.parse_player_ids({"a": {"foo": 1}})


def test_parse_league_info():
    info = load("fxea_getLeagueInfo.json")
    teams = fx.parse_teams(info)
    assert len(teams) == 10
    assert teams["tbb0000000000000"] == {"name": "The Blue Blazers", "short_name": "TBB"}  # from matchups
    scoring = fx.parse_scoring(info)
    assert scoring["skater"] == {
        "A": 1.5,
        "Blk": 0.08,
        "G": 2.0,
        "Hit": 0.08,
        "PIM": 0.1,
        "+/-": 0.25,
        "SOG": 0.1,
        "PPP": 0.5,
    }
    assert scoring["goalie"] == {"GA": -1.0, "SV": 0.155, "SHO": 2.0, "W": 2.0}
    assert fx.scoring_consistency(info) == []


def test_scoring_categories_string_form_is_a_fallback():
    info = load("fxea_getLeagueInfo.json")
    del info["scoringSystem"]["scoringCategorySettings"]
    scoring = fx.parse_scoring(info)
    assert scoring["goalie"]["SV"] == 0.155 and scoring["skater"]["+/-"] == 0.25


def test_scoring_copies_disagreeing_is_reported():
    info = load("fxea_getLeagueInfo.json")
    info["scoringSystem"]["scoringCategories"]["GOALIE"]["SV"]["Default"] = "points0.2"
    assert fx.scoring_consistency(info) == ["goalie SV: settings=0.155 categories=0.2"]


def test_position_specific_scoring_becomes_override():
    info = load("fxea_getLeagueInfo.json")
    skaters = info["scoringSystem"]["scoringCategorySettings"][0]["configs"]
    skaters.append({"points": 3.0, "position": {"code": "D"}, "scoringCategory": {"shortName": "G"}})
    assert fx.parse_scoring(info)["D"] == {"G": 3.0}


def test_roster_info_mismatch_reported():
    info = load("fxea_getLeagueInfo.json")
    league = {
        "roster": {"active": {"C": 2, "LW": 2, "RW": 2, "D": 4, "G": 2}, "reserve": 6, "max_players": 27}
    }
    assert fx.check_roster_info(info, league) == ["max players: Fantrax says 28, data/league.yaml says 27"]
    league["roster"]["max_players"] = 28
    assert fx.check_roster_info(info, league) == []


def test_parse_scoring_absent():
    assert fx.parse_scoring({"teamInfo": {}}) is None


def test_parse_scoring_keyed_by_code():
    info = {"scoring": {"skaters": {"G": {"points": 2}, "A": {"points": 1}}, "goalies": {"W": {"points": 5}}}}
    assert fx.parse_scoring(info) == {"skater": {"G": 2, "A": 1}, "goalie": {"W": 5}}


def test_parse_rosters():
    rows = fx.parse_rosters(load("fxea_getTeamRosters.json"))
    mine = [r for r in rows if r.team_id == "tbb0000000000000"]
    assert len(mine) == 9 and mine[0].team_name == "The Blue Blazers"
    assert {r.status for r in mine} == {"ACTIVE", "RESERVE", "INJURED_RESERVE", "MINORS"}


def test_parse_rosters_bad_shape():
    with pytest.raises(fx.FantraxShapeError):
        fx.parse_rosters({"teams": []})


def test_parse_player_ids_team_from_team_short_or_full_name():
    data = {
        "a1": {"fantraxId": "a1", "name": "Aho, Sebastian", "teamShortName": "CAR", "position": "C"},
        "b2": {"fantraxId": "b2", "name": "Hughes, Jack", "teamName": "New Jersey Devils", "position": "C"},
        "c3": {
            "fantraxId": "c3",
            "name": "Stützle, Tim",
            "teamShortName": "",
            "teamName": "Ottawa Senators",
            "position": "C",
            "rotowireId": "123",
            "statsIncId": "9",
            "shortName": "T. Stützle",
        },
        "d4": {"fantraxId": "d4", "name": "Prospect, Some", "teamShortName": "(N/A)", "position": "D"},
    }
    p = {x.fantrax_id: x for x in fx.parse_player_ids(data)}
    assert p["a1"].nhl_team == "CAR" and p["b2"].nhl_team == "NJD" and p["c3"].nhl_team == "OTT"
    assert p["d4"].nhl_team is None
    assert p["c3"].extra == {"rotowireId": "123", "statsIncId": "9"}
