"""Phase 3: periods, schedule filtering, the optimizer, availability, and the lineup report."""

import itertools
import random
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from typer.testing import CliRunner

from hockey import cli, db
from hockey.lineup.availability import availability
from hockey.lineup.optimize import SLOTS, Candidate, optimize
from hockey.lineup.periods import current_period, next_period, parse_periods
from hockey.lineup.report import build_report
from hockey.projection.inseason import Window, blend
from hockey.scoring.rules import build_rules
from hockey.sources.nhl import Game, NhlClient
from hockey.sync import load_rules
from hockey.valuation import Valuer
from tests.conftest import FakeHttp, load
from tests.test_sync_csv import full_sync

MINE = "tbb0000000000000"
PERIODS = parse_periods(load("fxea_getLeagueInfo.json")["rosterPeriods"])
P2 = PERIODS[1]
UTC = UTC


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# ------------------------------------------------------------------ periods + schedule


def test_periods_and_next_lock():
    assert [p.number for p in PERIODS] == [1, 2]
    assert P2.start == utc(2026, 10, 5, 23, 0)
    assert next_period(PERIODS, utc(2026, 9, 28)).number == 1
    assert next_period(PERIODS, utc(2026, 10, 5, 22, 59)).number == 2
    assert next_period(PERIODS, utc(2026, 10, 5, 23, 0)) is None  # period 2 just locked
    assert current_period(PERIODS, utc(2026, 10, 5, 23, 0)).number == 2
    assert parse_periods([{"number": 1, "startDate": "garbage"}]) == []


def test_schedule_only_counts_regular_season_games_inside_the_period():
    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    assert len(games) == 12
    pairs = {(g.away, g.home) for g in games}
    assert ("BOS", "OTT") in pairs  # 19:00 ET, exactly at the lock
    assert ("MTL", "TOR") not in pairs  # 18:59 ET, before it
    assert ("CAR", "WSH") not in pairs  # preseason game type
    assert ("VAN", "NYR") not in pairs  # after the period ends


# ------------------------------------------------------------------ optimizer


def brute_force(cands, slots):
    best = 0.0
    options = [[None] + [p for p in c.positions if p in slots] for c in cands]
    for choice in itertools.product(*options):
        used = {p: 0 for p in slots}
        ok = True
        for p in choice:
            if p:
                used[p] += 1
                ok = ok and used[p] <= slots[p]
        if ok:
            best = max(best, sum(c.value for c, p in zip(cands, choice, strict=True) if p))
    return best


@pytest.mark.parametrize("seed", range(25))
def test_dp_matches_brute_force(seed):
    rng = random.Random(seed)
    slots = {"C": 1, "LW": 1, "RW": 1, "D": 2, "G": 1}
    pool = [("C",), ("LW",), ("RW",), ("D",), ("G",), ("C", "LW"), ("RW", "C"), ("LW", "RW")]
    cands = [
        Candidate(str(i), rng.choice(pool), round(rng.uniform(0, 10), 2)) for i in range(rng.randint(3, 8))
    ]
    assert optimize(cands, slots).total == pytest.approx(brute_force(cands, slots))


def test_multi_position_player_fills_the_scarce_slot():
    cands = [Candidate("marner", ("RW", "C"), 10), Candidate("c1", ("C",), 9), Candidate("c2", ("C",), 8)]
    a = optimize(cands, {"C": 2, "RW": 1})
    assert a.slot_of == {"marner": "RW", "c1": "C", "c2": "C"} and a.total == 27


def test_empty_slots_and_zero_value_fill():
    a = optimize([Candidate("x", ("C",), 0.0), Candidate("g", ("G",), 3.0)], SLOTS)
    assert a.slot_of == {"x": "C", "g": "G"}  # a zero-game player still beats an empty slot
    assert a.empty == {"C": 1, "LW": 2, "RW": 2, "D": 4, "G": 1}


def test_optimizer_is_fast_on_a_full_roster():
    rng = random.Random(1)
    pool = [("C",), ("LW",), ("RW",), ("D",), ("G",), ("C", "LW"), ("RW", "C")]
    cands = [Candidate(str(i), rng.choice(pool), rng.uniform(0, 12)) for i in range(26)]
    t = time.perf_counter()
    optimize(cands)
    assert time.perf_counter() - t < 1.0


# ------------------------------------------------------------------ availability

RULES = build_rules({"skater": {"G": 2}, "goalie": {"W": 2}}, source="t")


def proj(pos, share, gs=1.0):
    rates = {"g": 0.4, "gs": gs, "w": 0.5, "sa": 28, "sv": 25.5}
    return blend(RULES, pos, prior=None, prior_rates=rates, prior_share=share)


def games_for(team, *hours):
    base = utc(2026, 10, 6, 23)
    return [Game(base + timedelta(hours=h), team, "XXX") for h in hours]


def test_skater_expected_games_use_games_share():
    av = availability(proj("F", 0.9), "CAR", games_for("CAR", 0, 48, 96), out_reason=None)
    assert av.games == 3 and av.expected == pytest.approx(2.7) and av.b2b == 0


def test_goalie_back_to_back_discount():
    starter = availability(proj("G", 0.8), "VAN", games_for("VAN", 0, 24, 96), out_reason=None)
    assert starter.b2b == 1 and starter.expected == pytest.approx(0.8 + 0.4 + 0.8)
    backup = availability(proj("G", 0.3), "VAN", games_for("VAN", 0, 24, 96), out_reason=None)
    assert backup.expected == pytest.approx(0.9)  # backups don't get the discount


def test_out_player_plays_nothing():
    av = availability(proj("F", 1.0), "CAR", games_for("CAR", 0, 48), out_reason="marked out (--out)")
    assert av.out and av.expected == 0 and av.games == 2


def test_players_absent_while_team_plays_are_flagged():
    p = blend(
        RULES, "F", prior=None, prior_rates={"g": 0.4}, prior_share=1.0, team_gp=15, season=Window(0, {})
    )
    av = availability(p, "CAR", games_for("CAR", 0), out_reason=None)
    assert any("hasn't played lately" in f for f in av.flags)


# ------------------------------------------------------------------ report


@pytest.fixture
def league(conn, settings):
    full_sync(conn, settings)
    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    return conn, settings, games


def report(conn, settings, games, outs=None):
    return build_report(Valuer(conn, load_rules(conn), settings.league), MINE, P2, games, outs)


def test_report_on_fixture_league(league):
    r = report(*league)
    starters = {w.row.name: w for w in r.starters}
    assert starters["Sebastian Aho"].avail.games == 4 and starters["Sebastian Aho"].avail.b2b == 1
    assert starters["Mitchell Marner"].slot == "RW"  # RW,C eligible, C already full
    assert r.empty == {"LW": 2, "RW": 1, "D": 2, "G": 1}
    assert r.moves_in == [] and r.moves_out == [] and r.gain == pytest.approx(0)
    assert any("Marcus Pettersson is in IR but looks healthy" in f for f in r.flags)
    assert any("2 empty LW slots" in f for f in r.flags)
    bench = {w.row.name: w for w in r.bench}
    assert bench["JT Miller"].avail.games == 0  # NYR don't play in period 2


def test_out_flag_swaps_in_the_best_reserve(league):
    conn, settings, games = league
    conn.execute("INSERT INTO roster_entry VALUES (?,?,?,?,?,?)", (MINE, "08new", "C", "RESERVE", "fxea", 0))
    r = report(conn, settings, games, outs=["tim stützle"])  # accent/case-insensitive match
    assert [w.row.name for w in r.moves_in] == ["Rookie Newby"]
    assert [w.row.name for w in r.moves_out] == ["Tim Stutzle"]
    assert r.gain > 0 and r.current_total < r.total


def test_minors_promotion_hint(league):
    conn, settings, games = league
    conn.execute("UPDATE fantrax_player SET positions='C,LW' WHERE fantrax_id='08new'")
    conn.execute("INSERT INTO roster_entry VALUES (?,?,?,?,?,?)", (MINE, "08new", "C", "MINORS", "fxea", 0))
    r = report(conn, settings, games)
    assert any("Consider promoting Rookie Newby from Minors" in f and "empty LW" in f for f in r.flags)


def test_too_many_reserves_is_flagged(league):
    conn, settings, games = league
    extra = (
        "05jhu",
        "06sla",
        "05lun",
        "05uxb",
        "074qj",
        "04qz4",
        "08new",
        "04abc",
        "03el6",
        "048x9",
        "02ore",
        "04dem",
    )
    for fid in extra:  # 19 lineup-eligible players: at least 7 must sit
        conn.execute("INSERT INTO roster_entry VALUES (?,?,?,?,?,?)", (MINE, fid, "C", "RESERVE", "fxea", 0))
    r = report(conn, settings, games)
    assert any("on reserve (max 6)" in f for f in r.flags)


def test_cli_lineup(tmp_path, monkeypatch, settings):
    path = tmp_path / "cli.db"
    monkeypatch.setenv("HOCKEY_DB", str(path))
    c = db.connect(path)
    full_sync(c, settings)
    c.close()
    fake_schedule = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    monkeypatch.setattr(NhlClient, "schedule", lambda self, start, end: fake_schedule)
    out = CliRunner().invoke(
        cli.app, ["lineup", "--period", "2", "--out", "Quinn Hughes"], env={"COLUMNS": "200"}
    )
    assert out.exit_code == 0, out.output
    for text in (
        "lineup for period 2",
        "Recommended actives",
        "Juuse Saros",
        "marked out (--out)",
        "Move Quinn Hughes to Reserve",
        "never makes them for you",
        "MoneyPuck.com",
    ):
        assert text in out.output, text
    bad = CliRunner().invoke(cli.app, ["lineup", "--period", "99"])
    assert bad.exit_code == 1 and "No matching roster period" in bad.output
