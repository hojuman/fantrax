"""Small NHL samples: a 9-GP call-up on an NHL roster isn't a 9-game player (Porter Martone's bug)."""

import json

import pytest

from hockey import valuation
from hockey.db import get_meta, set_meta
from hockey.projection.inseason import ON_ROSTER_SHARE, SEASON_GAMES
from hockey.sync import load_rules
from hockey.valuation import Valuer
from tests.test_sync_csv import full_sync

MCDAVID = 8478402
PROSPECT = 9900001


@pytest.fixture
def db_(conn, settings):
    full_sync(conn, settings)
    latest = get_meta(conn, "seasons")["completed"][0]
    src = conn.execute(
        "SELECT * FROM nhl_stat_season WHERE nhl_id=? AND season=?", (MCDAVID, latest)
    ).fetchone()
    stats = {k: v * 9 / src["gp"] for k, v in json.loads(src["stats"]).items()}
    conn.execute(
        "INSERT INTO nhl_player (nhl_id, full_name, pos_code, pos_group, team) VALUES (?, 'Pro Spect', 'R', 'F', 'PHI')",
        (PROSPECT,),
    )
    conn.execute(
        "INSERT INTO nhl_stat_season (nhl_id, season, pos_group, team, gp, stats) VALUES (?, ?, 'F', 'PHI', 9, ?)",
        (PROSPECT, latest, json.dumps(stats)),
    )
    return conn, settings


def valuer(conn, settings, on_roster: bool):
    ids = set(get_meta(conn, "nhl_roster_ids") or [])
    ids = ids | {PROSPECT} if on_roster else ids - {PROSPECT}
    set_meta(conn, "nhl_roster_ids", sorted(ids))
    return Valuer(conn, load_rules(conn), settings.league)


def test_prospect_on_an_nhl_roster_gets_role_based_games(db_):
    p = valuer(*db_, on_roster=True).project(PROSPECT)
    assert p.method == "prior only" and p.prior.sample_gp == 9
    w = 9 / 40
    expected = w * 9 / SEASON_GAMES + (1 - w) * ON_ROSTER_SHARE["F"]
    assert p.games_share == pytest.approx(expected) and p.games_share > 0.5  # was 9/82 = 0.11
    assert p.ros_gp == pytest.approx(expected * SEASON_GAMES)
    assert any("small NHL sample (9 GP)" in n and "on an NHL roster" in n for n in p.notes)


def test_prospect_off_roster_keeps_his_own_small_share(db_):
    p = valuer(*db_, on_roster=False).project(PROSPECT)
    assert p.games_share == pytest.approx(9 / SEASON_GAMES)
    assert any("not on an NHL roster" in n for n in p.notes)


def test_tiny_sample_rates_pulled_toward_the_rookie_baseline(db_, monkeypatch):
    conn, settings = db_
    new = valuer(conn, settings, on_roster=True).project(PROSPECT)
    monkeypatch.setattr(valuation, "SMALL_SAMPLE_GP", 0)  # the old behaviour: regress to the mean only
    old = valuer(conn, settings, on_roster=True).project(PROSPECT)
    assert new.fp_per_gp < old.fp_per_gp
    assert old.games_share == pytest.approx(9 / SEASON_GAMES)


def test_veteran_with_a_full_sample_is_unchanged(db_, monkeypatch):
    conn, settings = db_
    new = valuer(conn, settings, on_roster=True).project(MCDAVID)
    monkeypatch.setattr(valuation, "SMALL_SAMPLE_GP", 0)
    old = valuer(conn, settings, on_roster=True).project(MCDAVID)
    assert new.prior.sample_gp >= 40
    assert (new.fp_per_gp, new.games_share, new.ros_fp) == (old.fp_per_gp, old.games_share, old.ros_fp)
    assert not any("small NHL sample" in n for n in new.notes)


def test_no_nhl_record_gets_no_games(db_):
    conn, settings = db_
    p = valuer(conn, settings, on_roster=False).project_unknown("F")
    assert p.method == "rookie default" and p.games_share == 0 and p.ros_gp == 0


def test_keeper_value_uses_the_new_games_share(db_):
    from hockey.keeper.plan import Rules, build_candidate
    from hockey.valuation import RosterRow

    conn, settings = db_
    p = valuer(conn, settings, on_roster=True).project(PROSPECT)
    r = RosterRow("pro", "Pro Spect", "RW", "F", "PHI", "MINORS", None, PROSPECT, "exact", p)
    c = build_candidate(
        r, age_next=20.9, career_gp=9, known_career=True, entry=None, keeper_line=50.0, rules=Rules()
    )
    assert c.minors_eligible
    assert c.yearly[0] == pytest.approx(p.fp_per_gp * p.games_share * 82 * 1.10)  # aged from 19.9: F ×1.10
