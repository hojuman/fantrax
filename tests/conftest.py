"""Shared fixtures. Network is disabled for the whole suite (pytest-socket, see pyproject)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from hockey import db
from hockey.config import Settings, load_league_yaml
from hockey.http import Response, build_url, check_allowed

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeHttp:
    """Stands in for HttpClient: serves fixture files by URL, still enforcing the read-only guard."""

    def __init__(self, overrides: dict[str, object] | None = None, *, live: bool = False):
        """``live``: serve the 2026-27 in-season scenario (stats, recent windows, standings, MoneyPuck)."""
        self.overrides = overrides or {}
        self.live = live
        self.calls: list[str] = []
        self.stats = {"hits": 0, "fetches": 0}
        self.nhl_stats = load("nhl_stats.json") | (load("nhl_stats_live.json") if live else {})
        self.nhl_windows = load("nhl_windows.json") if live else {}
        self.nhl_rosters = load("nhl_rosters.json")

    def get(self, url, params=None, *, ttl=None, cache_if=None):
        full = build_url(url, params)
        check_allowed(full)
        self.calls.append(full)
        parts = urlsplit(full)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        for needle, body in self.overrides.items():
            if needle in full:
                return Response(full, 200, json.dumps(body), False)
        path = parts.path
        if "/fxea/general/" in path:
            method = path.rsplit("/", 1)[-1]
            return Response(full, 200, (FIXTURES / f"fxea_{method}.json").read_text(), False)
        if parts.hostname == "api.nhle.com":
            kind_report = "/".join(path.split("/")[-2:])
            cayenne = q["cayenneExp"]
            season = cayenne.split("seasonId=")[1].split()[0]
            if "gameDate>=" in cayenne:
                since = date.fromisoformat(cayenne.split('gameDate>="')[1][:10])
                until = date.fromisoformat(cayenne.split('gameDate<="')[1][:10])
                window = "last14" if (until - since).days < 20 else "last30"
                rows = self.nhl_windows.get(window, {}).get(kind_report, [])
            else:
                rows = self.nhl_stats.get(season, {}).get(kind_report, [])
            start, limit = int(q.get("start", 0)), int(q.get("limit", 100))
            return Response(
                full, 200, json.dumps({"data": rows[start : start + limit], "total": len(rows)}), False
            )
        if "/schedule/" in path:
            first = date.fromisoformat(path.rsplit("/", 1)[1])
            week = [
                d for d in load("nhl_schedule.json") if 0 <= (date.fromisoformat(d["date"]) - first).days < 7
            ]
            return Response(full, 200, json.dumps({"gameWeek": week}), False)
        if "/standings/" in path:
            return Response(full, 200, (FIXTURES / "nhl_standings.json").read_text(), False)
        if parts.hostname == "moneypuck.com":
            year, group = path.split("/")[-3], path.split("/")[-1].removesuffix(".csv")
            f = FIXTURES / "moneypuck" / f"{year}_{group}.csv"
            if f.exists() and (self.live or int(year) < 2026):
                return Response(full, 200, f.read_text(), False)
            return Response(full, 404, "not found", False)
        if "/roster/" in path:
            team = path.split("/")[-2]
            return Response(full, 200, json.dumps(self.nhl_rosters.get(team, {})), False)
        return Response(full, 404, "not found", False)

    def close(self):
        pass


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        league_id="testleague",
        my_team_name="The Blue Blazers",
        my_team_short="TBB",
        db_path=tmp_path / "t.db",
        league=load_league_yaml(),
    )


@pytest.fixture
def fake_http():
    return FakeHttp()
