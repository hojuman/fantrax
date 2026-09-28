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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hockey.http import FetchError, HttpClient, ReadOnlyViolation
from hockey.scoring.rules import ScoringConfigError, build_rules
from hockey.sources import fantrax_fxea as fxea
from hockey.sources import moneypuck as mp
from hockey.sources.nhl import STATS, WEB, completed_seasons, current_season

PERSONAL_KEY = re.compile(r"owner|manager|email|user|commish|commissioner|nick|secret", re.I)
KEEP_NAMES = ("aho", "pettersson")  # ID-mapping traps worth keeping in samples
SAMPLE_PLAYER = "mcdavid"  # a player who'll always be in the feed; his raw entry exposes renames
TERMS_RE = re.compile(r"commercial|credit|licen|permission|terms|allowed|free to use|attribution", re.I)
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


def terms_sentences(page: str, limit: int = 6) -> list[str]:
    """Sentences about usage terms from an HTML page (style/script bodies removed first)."""
    page = re.sub(r"<(head|style|script)\b[^>]*>.*?</\1\s*>", " ", page, flags=re.I | re.S)
    page = re.sub(r"</?(p|div|li|h[1-6]|br|tr|td|title)\b[^>]*>", " . ", page, flags=re.I)  # block = break
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
    sentences = (s.strip(" .") for s in re.split(r"(?<=[.!?])\s+", text))
    return [s + "." for s in sentences if s and TERMS_RE.search(s)][:limit]


def describe_scoring(scoring: dict[str, dict[str, float]] | None, league: dict | None = None) -> str:
    """Parsed scoring table plus whether our rules layer accepts every code."""
    if not scoring:
        return "scoring: NOT FOUND in getLeagueInfo (fill data/league.yaml)"
    table = "; ".join(
        f"{g}: " + ", ".join(f"{c}={w:g}" for c, w in codes.items()) for g, codes in scoring.items()
    )
    cfg = (league or {}).get("scoring") or {}
    try:
        build_rules(
            scoring,
            source="fantrax",
            code_aliases=cfg.get("code_aliases"),
            ignore_codes=cfg.get("ignore_codes"),
        )
    except ScoringConfigError as e:
        return f"scoring: {table} | MAPPING FAILED: {e}"
    return f"scoring: {table} | all codes mapped ✓"


def sample_player_entry(data: Any, needle: str = SAMPLE_PLAYER) -> str:
    items = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
    for v in items:
        if isinstance(v, dict) and needle in str(v.get("name", "")).lower():
            return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return f"no entry containing {needle!r}"


def value_counts(rows: list[dict], key: str) -> str:
    c = Counter(str(r.get(key)) for r in rows if isinstance(r, dict))
    return ", ".join(f"{k}×{n}" for k, n in c.most_common(10)) or "none"


def _roster_items(rosters: Any) -> list[dict]:
    teams = rosters.get("rosters") if isinstance(rosters, dict) else None
    teams = teams.values() if isinstance(teams, dict) else teams or []
    return [i for t in teams if isinstance(t, dict) for i in (t.get("rosterItems") or t.get("players") or [])]


def run_probe(
    http: HttpClient, league_id: str | None, out_dir: Path, today, league: dict | None = None
) -> list[ProbeResult]:
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
                f"{len(players)} players parsed; extra fields: {extra_keys or 'none'}; "
                f"with an NHL team: {sum(1 for p in players if p.nhl_team)} | "
                f"sample: {sample_player_entry(data)}",
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
                    f"{_shape(info)} | teams parsed: {len(teams)}, pool: {len(pool)} "
                    f"(status: {value_counts(list(pool.values()), 'status')}) | "
                    f"{describe_scoring(scoring, league)} | rosterInfo: {json.dumps(info.get('rosterInfo'))[:300]}",
                )
            )
        rosters = get_json("fxea_getTeamRosters", f"{fxea.BASE}/getTeamRosters", {"leagueId": league_id})
        if rosters is not None:
            try:
                n = len(fxea.parse_rosters(rosters))
                results.append(
                    ProbeResult(
                        "fxea getTeamRosters",
                        True,
                        "200",
                        f"{_shape(rosters)} | {n} roster rows parsed | "
                        f"status: {value_counts(_roster_items(rosters), 'status')} | "
                        f"position: {value_counts(_roster_items(rosters), 'position')} | "
                        f"item keys: {sorted(_roster_items(rosters)[0]) if _roster_items(rosters) else []}",
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
    cur = current_season(today)
    d = get_json("nhl_roster_VAN", f"{WEB}/roster/VAN/{cur}")
    if d is not None:
        results.append(ProbeResult(f"NHL roster/VAN/{cur}", True, "200", _shape(d)))
    d = get_json("nhl_schedule", f"{WEB}/schedule/{today.isoformat()}")
    if d is not None:
        from hockey.sources.nhl import parse_schedule

        days = d.get("gameWeek") or []
        raw = [g for day in days for g in (day.get("games") or [])]
        parsed = parse_schedule(d)
        first = sorted(raw[0]) if raw else []
        results.append(
            ProbeResult(
                f"NHL schedule/{today.isoformat()}",
                bool(days),
                "200",
                f"{len(days)} days, {len(raw)} games ({len(parsed)} regular season parsed); "
                f"game fields: {', '.join(first[:12]) or 'none'}",
            )
        )

    # --- MoneyPuck: terms first; data is only used after you've read these.
    try:
        resp = http.get("https://moneypuck.com/data.htm", ttl=0)
        sentences = terms_sentences(resp.text)
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

    # MoneyPuck season files: do the columns we read still exist?
    year = season // 10000
    for group, parser in (("skaters", mp.parse_skaters), ("goalies", mp.parse_goalies)):
        name = f"MoneyPuck {year}/{group}.csv"
        try:
            resp = http.get(f"{mp.BASE}/{year}/regular/{group}.csv", ttl=0)
            if resp.status != 200:
                results.append(ProbeResult(name, False, f"HTTP {resp.status}", resp.text[:120]))
                continue
            header = resp.text.split("\n", 1)[0]
            lines = parser(resp.text, season)
            results.append(
                ProbeResult(
                    name, True, "200", f"{len(lines)} players parsed; {header.count(',') + 1} columns"
                )
            )
        except mp.MoneyPuckShapeError as e:
            results.append(ProbeResult(name, False, "columns", str(e)))
        except (FetchError, ReadOnlyViolation) as e:
            results.append(ProbeResult(name, False, "error", str(e)))

    d = get_json("nhl_standings", f"{WEB}/standings/{today.isoformat()}")
    if d is not None:
        from hockey.sources.nhl import parse_standings

        gp = parse_standings(d)
        results.append(
            ProbeResult(
                f"NHL standings/{today.isoformat()}",
                bool(gp),
                "200",
                f"{len(gp)} teams; games played {min(gp.values(), default=0)}–"
                f"{max(gp.values(), default=0)} (0 or last season's 82 before opening night)",
            )
        )
    return results
