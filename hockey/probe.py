"""`hockey probe`: hit each data source once and report what actually comes back.

Run it after the network is set up and whenever something breaks. It prints status and shape per
endpoint, tells you whether our parsers understood the response, and saves *sanitized, truncated*
samples to var/probe/ (gitignored) so fixtures can be refreshed deliberately, never by accident.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hockey.http import FetchError, HttpClient, ReadOnlyViolation
from hockey.sources import fantrax_fxea as fxea
from hockey.sources.nhl import STATS, WEB, completed_seasons

PERSONAL_KEY = re.compile(r"owner|manager|email|user|commish|commissioner|nick|secret", re.I)
KEEP_NAMES = ("aho", "pettersson")  # ID-mapping traps worth keeping in samples
MAX_ITEMS = 25


@dataclass
class ProbeResult:
    name: str
    ok: bool
    status: str
    detail: str


def _hash(s: str) -> str:
    return "anon-" + hashlib.sha256(s.encode()).hexdigest()[:8]


def sanitize(node: Any, key: str = "") -> Any:
    """Hash personal-looking fields; truncate large collections (keeping ID-mapping traps)."""
    if isinstance(node, dict):
        items = list(node.items())
        if len(items) > MAX_ITEMS:
            keep = [kv for kv in items if any(n in json.dumps(kv[1]).lower() for n in KEEP_NAMES)]
            items = items[:MAX_ITEMS] + [kv for kv in keep if kv not in items[:MAX_ITEMS]]
        return {k: sanitize(v, k) for k, v in items}
    if isinstance(node, list):
        return [sanitize(v, key) for v in node[:MAX_ITEMS]]
    if isinstance(node, str) and PERSONAL_KEY.search(key):
        return _hash(node)
    return node


def _shape(data: Any) -> str:
    if isinstance(data, dict):
        return f"object, {len(data)} keys: {', '.join(list(data)[:8])}{'…' if len(data) > 8 else ''}"
    if isinstance(data, list):
        return f"list of {len(data)}"
    return type(data).__name__


def run_probe(http: HttpClient, league_id: str | None, out_dir: Path, today) -> list[ProbeResult]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ProbeResult] = []

    def save(name: str, data: Any) -> None:
        (out_dir / f"{name}.json").write_text(json.dumps(sanitize(data), indent=1, sort_keys=True))

    def get_json(name: str, url: str, params: dict | None = None) -> Any | None:
        try:
            resp = http.get(url, params, ttl=0)
        except (FetchError, ReadOnlyViolation) as e:
            results.append(ProbeResult(name, False, "error", str(e)))
            return None
        if resp.status != 200:
            results.append(ProbeResult(name, False, f"HTTP {resp.status}", resp.text[:160]))
            return None
        try:
            data = resp.json()
        except ValueError:
            results.append(ProbeResult(name, False, "not JSON", resp.text[:160]))
            return None
        save(name, data)
        err = fxea.body_error(data) if "fantrax" in url else None
        if err:
            results.append(ProbeResult(name, False, "200 + error body", err))
            return None
        return data

    # --- Fantrax (published fxea API only)
    data = get_json("fxea_getPlayerIds", f"{fxea.BASE}/getPlayerIds", {"sport": "NHL"})
    if data is not None:
        players = fxea.parse_player_ids(data)
        extra_keys = sorted({k for p in players for k in p.extra})
        results.append(
            ProbeResult(
                "fxea getPlayerIds",
                True,
                "200",
                f"{len(players)} players parsed; extra fields: {extra_keys or 'none'} "
                "(an NHL id here would make ID mapping trivial)",
            )
        )
    if league_id:
        info = get_json("fxea_getLeagueInfo", f"{fxea.BASE}/getLeagueInfo", {"leagueId": league_id})
        if info is not None:
            teams, pool, scoring = fxea.parse_teams(info), fxea.parse_pool(info), fxea.parse_scoring(info)
            results.append(
                ProbeResult(
                    "fxea getLeagueInfo",
                    True,
                    "200",
                    f"{_shape(info)} | teams parsed: {len(teams)}, pool: {len(pool)}, "
                    f"scoring codes: { ({g: len(c) for g, c in scoring.items()} if scoring else 'NOT FOUND') }",
                )
            )
        rosters = get_json("fxea_getTeamRosters", f"{fxea.BASE}/getTeamRosters", {"leagueId": league_id})
        if rosters is not None:
            try:
                n = len(fxea.parse_rosters(rosters))
                results.append(
                    ProbeResult(
                        "fxea getTeamRosters", True, "200", f"{_shape(rosters)} | {n} roster rows parsed"
                    )
                )
            except fxea.FantraxShapeError as e:
                results.append(ProbeResult("fxea getTeamRosters", False, "shape", str(e)))
        st = get_json("fxea_getStandings", f"{fxea.BASE}/getStandings", {"leagueId": league_id})
        if st is not None:
            results.append(ProbeResult("fxea getStandings", True, "200", _shape(st)))
    else:
        results.append(
            ProbeResult("fxea league endpoints", False, "skipped", "FANTRAX_LEAGUE_ID not set in .env")
        )

    # --- NHL
    season = completed_seasons(today, 1)[0]
    for kind, report in [
        ("skater", "summary"),
        ("skater", "realtime"),
        ("skater", "faceoffwins"),
        ("goalie", "summary"),
    ]:
        d = get_json(
            f"nhl_{kind}_{report}",
            f"{STATS}/{kind}/{report}",
            {
                "isAggregate": "false",
                "isGame": "false",
                "start": 0,
                "limit": 5,
                "cayenneExp": f"gameTypeId=2 and seasonId={season}",
            },
        )
        if d is not None:
            row = (d.get("data") or [{}])[0]
            results.append(
                ProbeResult(
                    f"NHL {kind}/{report}",
                    True,
                    "200",
                    f"total={d.get('total')} fields: {', '.join(sorted(row)[:12])}…",
                )
            )
    d = get_json("nhl_roster_VAN", f"{WEB}/roster/VAN/current")
    if d is not None:
        results.append(ProbeResult("NHL roster/VAN/current", True, "200", _shape(d)))
    d = get_json("nhl_schedule_now", f"{WEB}/schedule/now")
    if d is not None:
        results.append(ProbeResult("NHL schedule/now", True, "200", _shape(d)))

    # --- MoneyPuck: terms first; data is only used after you've read these.
    try:
        resp = http.get("https://moneypuck.com/data.htm", ttl=0)
        text = html.unescape(re.sub(r"<[^>]+>", " ", resp.text))
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
            if re.search(r"commercial|credit|licen|permission|terms|allowed|free to use|attribut", s, re.I)
        ]
        results.append(
            ProbeResult(
                "MoneyPuck data.htm (terms)",
                resp.status == 200,
                f"HTTP {resp.status}",
                " | ".join(sentences[:6]) or "no terms language found; check the page by hand",
            )
        )
    except (FetchError, ReadOnlyViolation) as e:
        results.append(ProbeResult("MoneyPuck data.htm (terms)", False, "error", str(e)))
    return results
