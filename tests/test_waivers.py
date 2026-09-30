"""Phase 4: waiver / free-agent report."""

import time
from datetime import date

import pytest
from typer.testing import CliRunner

from hockey import cli, db
from hockey.keepers import load_keepers
from hockey.sources.nhl import NhlClient
from hockey.sync import load_rules
from hockey.valuation import RosterRow, Valuer
from hockey.waivers.report import build_waivers, open_spots, schedule_density
from tests.conftest import FakeHttp
from tests.test_lineup import P2
from tests.test_sync_csv import full_sync

MINE = "tbb0000000000000"
ROSTER = {"active": {"C": 2, "LW": 2, "RW": 2, "D": 4, "G": 2}, "reserve": 6, "effective_max": 26}
FULL = {
    **ROSTER,
    "effective_max": 9,
}  # the fixture roster has 9 players: no open spot, every pickup needs a drop


@pytest.fixture
def league(conn, settings):
    full_sync(conn, settings)
    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    return conn, settings, games


def waivers(conn, settings, games, roster=ROSTER, **kw):
    return build_waivers(Valuer(conn, load_rules(conn), settings.league), MINE, P2, games, roster, **kw)


def row(status):
    return RosterRow("x", "x", "C", "F", None, status, None, None, None, None)


def test_open_spots_uses_both_limits():
    lineup = [row("ACTIVE")] * 12 + [row("RESERVE")] * 5
    assert open_spots(lineup, ROSTER) == 1  # 18 - 17 lineup players
    assert open_spots(lineup + [row("MINORS")] * 5 + [row("INJURED_RESERVE")] * 3, ROSTER) == 1
    assert open_spots(lineup + [row("MINORS")] * 5 + [row("INJURED_RESERVE")] * 4, ROSTER) == 0  # 26 total
    assert open_spots([row("ACTIVE")] * 12 + [row("RESERVE")] * 6, ROSTER) == 0


def test_pickup_gain_is_the_lineup_improvement(league):
    r = waivers(*league)
    top = r.pickups[0]
    assert top.pickup.row.name == "Juraj Slafkovsky" and top.drop is None  # open spot
    assert top.gain == pytest.approx(top.pickup.ros, rel=1e-6)  # fills an empty LW slot
    assert all(o.pickup.row.name != "Jack Hughes" for o in r.pickups)  # would only sit behind Aho/Stutzle
    lw = next(p for p in r.by_position if p.pos == "LW")
    assert lw.weakest is None and lw.best.pickup.row.name == "Juraj Slafkovsky"
    c = next(p for p in r.by_position if p.pos == "C")
    assert c.weakest.row.name in ("Sebastian Aho", "Tim Stutzle") and c.best is None


def test_full_roster_picks_the_cheapest_drop(league):
    r = waivers(*league, roster=FULL)
    top = r.pickups[0]
    assert top.pickup.row.name == "Juraj Slafkovsky"
    assert top.drop.row.name == "JT Miller"  # bench-only: costs the lineup nothing
    assert top.gain == pytest.approx(top.pickup.ros, rel=1e-6)


def test_protected_players_are_never_dropped(league, tmp_path):
    r = waivers(*league, roster=FULL, protect=["JT Miller"])
    assert r.pickups[0].drop.row.name != "JT Miller"
    kp = tmp_path / "keepers.yaml"
    kp.write_text(
        "players:\n  - {player: JT Miller, fantrax_id: '03mil', times_kept: 0, franchise_tag: true}\n"
    )
    r2 = waivers(*league, roster=FULL, keepers=load_keepers(kp))
    assert r2.pickups[0].drop.row.name != "JT Miller"
    for o in r2.pickups + r2.streamers:
        assert o.drop.status in ("ACTIVE", "RESERVE")  # never IR (Marcus Pettersson) or Minors (prospect)


def test_keeper_history_note(league, tmp_path):
    kp = tmp_path / "keepers.yaml"
    kp.write_text("players:\n  - {player: JT Miller, fantrax_id: '03mil', times_kept: 2}\n")
    r = waivers(*league, roster=FULL, keepers=load_keepers(kp))
    assert any("JT Miller has keeper history" in n for n in r.notes)


def test_no_record_prospects_excluded_and_waivers_marked(league):
    conn, settings, games = league
    conn.execute("UPDATE fantrax_player SET pool_status='W (Tue)' WHERE fantrax_id='06sla'")
    r = waivers(conn, settings, games)
    names = {o.pickup.row.name for o in r.pickups + r.streamers}
    assert not names & {"Karri Aho", "Hugo Pettersson", "Ryan OReilly"}
    assert any("4 free agents have no NHL record" in n for n in r.notes)
    assert r.pickups[0].pickup.row.owner == "W" and any("on waivers" in n for n in r.notes)


def test_streamers_use_this_weeks_games(league):
    conn, settings, games = league
    conn.execute(
        "UPDATE roster_entry SET status='INJURED_RESERVE' WHERE team_id=? AND fantrax_id='05stu'", (MINE,)
    )
    r = waivers(conn, settings, games, roster=FULL, max_ros_cost=1000)
    top = r.streamers[0]
    assert top.pickup.row.name in ("Jack Hughes", "Rookie Newby") and top.drop.row.name == "JT Miller"
    assert top.gain == pytest.approx(top.pickup.week, rel=1e-6)  # Miller's NYR have no games: pure gain
    assert top.pickup.avail.games > 0


def test_streamers_dont_cut_real_players_for_one_week(league):
    conn, settings, games = league
    conn.execute(
        "UPDATE roster_entry SET status='INJURED_RESERVE' WHERE team_id=? AND fantrax_id='05stu'", (MINE,)
    )
    r = waivers(conn, settings, games, roster=FULL)  # default: at most 10 ROS points
    assert all(o.ros_change >= -10 for o in r.streamers)
    assert any("held back" in n and "--max-ros-cost" in n for n in r.notes)


def test_schedule_density(league):
    density = schedule_density(league[2])
    assert density[4] == ["CAR"] and density[2] == ["PIT", "VAN"] and density[1] == ["BOS"]


def test_fast_enough(league):
    t = time.perf_counter()
    waivers(*league, roster=FULL)
    assert time.perf_counter() - t < 2.0


def test_cli_waivers(tmp_path, monkeypatch, settings):
    path = tmp_path / "cli.db"
    monkeypatch.setenv("HOCKEY_DB", str(path))
    c = db.connect(path)
    full_sync(c, settings)
    c.close()
    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    monkeypatch.setattr(NhlClient, "schedule", lambda self, start, end: games)
    monkeypatch.setattr(cli, "HttpClient", lambda conn, refresh=False: FakeHttp())  # NHL career lookups
    out = CliRunner().invoke(cli.app, ["waivers", "--period", "2"], env={"COLUMNS": "220"})
    assert out.exit_code == 0, out.output
    for text in (
        "waiver report",
        "Juraj Slafkovsky",
        "By position",
        "Streamers for period 2",
        "Games per team this period",
        "make any claims or drops yourself",
        "MoneyPuck.com",
        "Keeper value lost",
    ):
        assert text in out.output, text
    assert "Keeper values unavailable" not in out.output
    assert CliRunner().invoke(cli.app, ["waivers", "--pos", "X"]).exit_code == 2
    d = CliRunner().invoke(cli.app, ["waivers", "--period", "2", "--pos", "D"], env={"COLUMNS": "200"})
    assert d.exit_code == 0 and "Juraj Slafkovsky" not in d.output


def test_open_spot_depth_list_when_nothing_improves_the_lineup(league):
    r = waivers(*league, pos="C")  # C slots are full with Aho + Stutzle
    assert r.open_spots > 0 and r.pickups == []
    assert [p.row.name for p in r.depth][:1] == ["Jack Hughes"]  # the best bench C by ROS
    assert all("C" in p.eligible for p in r.depth)
    full = waivers(*league)  # an LW pickup does help: no depth list needed
    assert full.pickups and full.depth == []


# ------------------------------------------------------------------ keeper value on drops

JT_MILLER = "03mil"  # the fixture's cheapest drop


def ir_hughes(league):
    conn, settings, games = league
    conn.execute(
        "UPDATE roster_entry SET status='INJURED_RESERVE' WHERE team_id=? AND fantrax_id='05stu'", (MINE,)
    )
    return conn, settings, games


def test_keeper_value_steers_pickups_away_from_a_prospect(league):
    plain = waivers(*league, roster=FULL)
    assert plain.pickups[0].drop.id == JT_MILLER
    r = waivers(*league, roster=FULL, keeper_costs={JT_MILLER: 400.0})  # more than the pickup is worth
    assert r.pickups and all(o.drop is None or o.drop.id != JT_MILLER for o in r.pickups)
    assert r.pickups[0].drop.row.name == "Sebastian Aho" and r.pickups[0].keeper_cost == 0


def test_streamers_never_cost_a_keeper(league):
    conn, settings, games = ir_hughes(league)
    plain = waivers(conn, settings, games, roster=FULL, max_ros_cost=1000)
    assert plain.streamers[0].drop.id == JT_MILLER  # the Martone bug: cheapest ROS = default drop
    r = waivers(conn, settings, games, roster=FULL, max_ros_cost=1000, keeper_costs={JT_MILLER: 127.1})
    assert r.streamers and all(o.drop.id != JT_MILLER for o in r.streamers)
    # A small keeper cost is allowed, shown, and charged at the keeper weight.
    cheap = waivers(conn, settings, games, roster=FULL, max_ros_cost=1000, keeper_costs={JT_MILLER: 2.0})
    top = cheap.streamers[0]
    assert top.drop.id == JT_MILLER and top.keeper_cost == 2.0
    assert top.net == pytest.approx(top.gain - 1.0)


def test_no_move_when_the_gain_doesnt_beat_the_keeper_value(league):
    conn, settings, games = ir_hughes(league)
    mine = [r.name for r in Valuer(conn, load_rules(conn), settings.league).team_roster(MINE)]
    others = [n for n in mine if n != "JT Miller"]
    r = waivers(
        conn, settings, games, roster=FULL, protect=others, max_ros_cost=1000, keeper_costs={JT_MILLER: 127.1}
    )
    assert r.streamers == []
    note = next(n for n in r.notes if "held back" in n)
    assert "for JT Miller" in note and "vs keeper value lost 127" in note


def test_unknown_keeper_value_is_never_a_drop(league):
    r = waivers(*league, roster=FULL, keeper_costs={JT_MILLER: None})
    assert all(o.drop is None or o.drop.id != JT_MILLER for o in r.pickups + r.streamers)
    assert any("Never suggested as drops" in n and "JT Miller" in n for n in r.notes)


def test_context_prices_drops_with_the_keeper_plan(league, settings):
    from hockey.context import LeagueContext, my_team

    conn, _, games = league
    ctx = LeagueContext(settings, conn, FakeHttp(), my_team(conn, settings))
    costs = ctx.keeper_costs()
    assert set(costs) >= {JT_MILLER} and all(v is None or v >= 0 for v in costs.values())
    assert costs["07pro"] is None  # the fixture prospect with no NHL record
