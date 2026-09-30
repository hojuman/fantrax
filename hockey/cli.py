"""`hockey` CLI. Read-only: prints recommendations, never acts on Fantrax."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date
from pathlib import Path

import typer
from rich.table import Table

from hockey import db
from hockey.config import REPO_ROOT, ConfigError, load_settings
from hockey.http import FetchError, HttpClient
from hockey.sources.fantrax_fxea import FantraxError, FantraxShapeError
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
from hockey.views.tables import console, kv_table, player_tables, rank_table, roster_table

app = typer.Typer(
    help="Read-only fantasy hockey assistant for Talladega Nights (Fantrax).", no_args_is_help=True
)


def _open(refresh: bool = False):
    settings = load_settings()
    conn = db.connect(settings.db_path)
    return settings, conn, HttpClient(conn, refresh=refresh)


def _print_report(report: SyncReport, http: HttpClient | None = None) -> None:
    counts = dict(report.counts)
    if http:
        counts["HTTP cache hits / fetches"] = f"{http.stats['hits']} / {http.stats['fetches']}"
    console.print(kv_table("Sync summary", counts))
    for n in report.notes:
        console.print(f"[cyan]•[/] {n}")
    for p in report.problems:
        console.print(f"[bold red]![/] {p}")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )


@app.command()
def sync(
    refresh: bool = typer.Option(False, help="Ignore the cache and re-fetch everything."),
    skip_nhl: bool = typer.Option(False, help="Only sync Fantrax + ID mapping."),
    skip_moneypuck: bool = typer.Option(False, help="Skip MoneyPuck (xG / ice time) data."),
) -> None:
    """Pull Fantrax league data and NHL stats, then map player ids."""
    settings, conn, http = _open(refresh)
    report = SyncReport()
    try:
        sync_fantrax(conn, http, settings, report)
        if not skip_nhl:
            sync_nhl(conn, http, date.today(), report)
            if not skip_moneypuck:
                sync_moneypuck(conn, http, date.today(), report)
        run_idmap(conn, report)
    except ConfigError as e:
        console.print(f"[bold red]{e}[/]")
        raise typer.Exit(2) from e
    except FetchError as e:
        console.print(f"[bold red]Network error:[/] {e}\nRun `hockey probe` to see which source is failing.")
        raise typer.Exit(1) from e
    except (FantraxError, FantraxShapeError) as e:
        console.print(f"[bold red]Fantrax:[/] {e}\nRun `hockey probe` to inspect the response.")
        raise typer.Exit(1) from e
    finally:
        http.close()
    _print_report(report, http)


@app.command("import-csv")
def import_csv_cmd(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV exported from Fantrax."),
    team: str = typer.Option(None, help="Put every row on this team (for a single-team roster export)."),
) -> None:
    """Fallback: import rosters / free agents from a Fantrax CSV export, then re-map ids."""
    settings, conn, http = _open()
    counts = import_csv(conn, path, team)
    report = SyncReport(counts={"CSV rows": counts["rows"], "rostered": counts["rostered"]})
    run_idmap(conn, report)
    _print_report(report)


@app.command()
def probe() -> None:
    """Check each data source live and save sanitized samples to var/probe/."""
    from hockey.probe import run_probe

    settings, conn, http = _open(refresh=True)
    out = REPO_ROOT / "var" / "probe"
    try:
        results = run_probe(http, settings.league_id or None, out, date.today(), settings.league)
    finally:
        http.close()
    t = Table(title="Data source probe", title_justify="left")
    t.add_column("Endpoint", no_wrap=True)
    t.add_column("OK")
    t.add_column("Status", no_wrap=True)
    t.add_column("Detail")
    for r in results:
        t.add_row(r.name, "[green]✓[/]" if r.ok else "[red]✗[/]", r.status, r.detail)
    console.print(t)
    console.print(
        f"Sanitized samples saved to {out} (gitignored). Review before copying any into tests/fixtures/."
    )


@app.command()
def ids(
    unmatched: bool = typer.Option(False, "--unmatched", help="List players with no NHL id."),
    fuzzy: bool = typer.Option(False, "--fuzzy", help="List fuzzy matches that need confirming."),
    all_players: bool = typer.Option(
        False,
        "--all",
        help="With --unmatched: include unrostered pool players (thousands, mostly juniors/Europeans).",
    ),
) -> None:
    """ID-mapping health: counts by method, unmatched and fuzzy lists."""
    settings, conn, http = _open()
    counts = {
        r["method"]: r["n"] for r in conn.execute("SELECT method, COUNT(*) n FROM player_map GROUP BY method")
    }
    rostered_sql = "EXISTS(SELECT 1 FROM roster_entry re WHERE re.fantrax_id = u.fantrax_id)"
    counts["unmatched: rostered"] = conn.execute(
        f"SELECT COUNT(*) FROM id_unmatched u WHERE {rostered_sql}"
    ).fetchone()[0]
    counts["unmatched: pool, no NHL record"] = conn.execute(
        f"SELECT COUNT(*) FROM id_unmatched u WHERE NOT {rostered_sql}"
    ).fetchone()[0]
    console.print(kv_table("Fantrax → NHL id mapping", counts))
    if unmatched:
        t = Table(
            title="Unmatched" + ("" if all_players else ": rostered players (--all for the pool too)"),
            title_justify="left",
        )
        for c in ("FX id", "Name", "Team", "Pos", "Rostered", "Reason", "Nearest candidates"):
            t.add_column(c)
        q = (
            f"SELECT u.*, {rostered_sql} AS rostered FROM id_unmatched u"
            + ("" if all_players else f" WHERE {rostered_sql}")
            + " ORDER BY rostered DESC, name"
        )
        for r in conn.execute(q):
            cands = "; ".join(f"{i}: {n}" for i, n in json.loads(r["candidates"] or "[]"))
            t.add_row(
                r["fantrax_id"],
                r["name"],
                r["nhl_team"] or "",
                r["pos_group"] or "",
                "yes" if r["rostered"] else "",
                r["reason"],
                cands,
            )
        console.print(t)
        console.print(
            "Fix with a row in data/id_overrides.csv (fantrax_id,nhl_id,note), then `hockey sync --skip-nhl`."
        )
    if fuzzy:
        t = Table(title="Fuzzy matches (confirm by copying into data/id_overrides.csv)", title_justify="left")
        for c in ("FX id", "Fantrax name", "NHL id", "NHL name", "NHL team"):
            t.add_column(c)
        for r in conn.execute(
            """SELECT pm.fantrax_id, fp.name fx_name, pm.nhl_id, np.full_name, np.team FROM player_map pm
               JOIN fantrax_player fp USING(fantrax_id) JOIN nhl_player np USING(nhl_id)
               WHERE pm.method='fuzzy' ORDER BY fp.name"""
        ):
            t.add_row(r["fantrax_id"], r["fx_name"], str(r["nhl_id"]), r["full_name"], r["team"] or "")
        console.print(t)


@app.command()
def roster(team: str = typer.Option(None, help="Team name or short name (default: MY_TEAM_NAME).")) -> None:
    """Print a roster with projected fantasy value under this league's scoring."""
    settings, conn, http = _open()
    if team:
        settings.my_team_name, settings.my_team_short = team, team
    row = find_my_team(conn, settings)
    if row is None:
        names = [r["name"] for r in conn.execute("SELECT name FROM fantasy_team ORDER BY name")]
        console.print(
            f"[red]Team {settings.my_team_name!r} not found.[/] Known teams: {names or 'none; run `hockey sync`'}"
        )
        raise typer.Exit(1)
    rules = load_rules(conn)
    rows = Valuer(conn, rules, settings.league).team_roster(row["team_id"])
    if not rows:
        console.print(
            f"[yellow]No roster rows for {row['name']}. Run `hockey sync` or `hockey import-csv`.[/]"
        )
        raise typer.Exit(1)
    console.print(roster_table(row["name"], rows, rules.source))
    if rules.is_empty():
        console.print(
            "[bold red]![/] No scoring weights yet: fill in data/league.yaml scoring and re-run `hockey sync`."
        )
    bad = [r.name for r in rows if r.nhl_id is None]
    if bad:
        console.print(
            f"[yellow]No NHL record (rookie estimate, 0 games projected): {', '.join(bad)}[/]. See `hockey ids --unmatched`."
        )


POSITIONS = ("C", "LW", "RW", "D", "G", "F")


def _pos_ok(positions: str, pos_group: str | None, want: str) -> bool:
    if want == "F":
        return pos_group == "F"
    return want in {p.strip().upper() for p in positions.split(",")}


@app.command()
def rank(
    pos: str = typer.Option(None, help="C, LW, RW, D, G or F (eligibility, not just primary position)."),
    available: bool = typer.Option(False, "--available", "-a", help="Only free agents / waivers."),
    owner: str = typer.Option(None, help="Only players on this fantasy team (name or short name)."),
    sort: str = typer.Option("fp", help="fp = points per game; ros = rest-of-season points."),
    limit: int = typer.Option(25, help="Rows to show."),
    min_games: float = typer.Option(0, help="Hide players projected for fewer rest-of-season games."),
) -> None:
    """League-wide ranking under this league's scoring (use --available for pickups)."""
    settings, conn, http = _open()
    if pos and pos.upper() not in POSITIONS:
        console.print(f"[red]--pos must be one of {', '.join(POSITIONS)}[/]")
        raise typer.Exit(2)
    rules = load_rules(conn)
    rows = [r for r in Valuer(conn, rules, settings.league).league_players() if r.projection]
    if pos:
        rows = [r for r in rows if _pos_ok(r.positions, r.pos_group, pos.upper())]
    if available:
        rows = [r for r in rows if r.owner in ("FA", "W")]
    if owner:
        team = conn.execute(
            "SELECT name FROM fantasy_team WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)",
            (owner, owner),
        ).fetchone()
        if team is None:
            console.print(f"[red]No fantasy team {owner!r}.[/]")
            raise typer.Exit(1)
        rows = [r for r in rows if r.owner == team["name"]]
    if min_games:
        rows = [r for r in rows if r.projection.ros_gp >= min_games]
    rows.sort(key=lambda r: r.projection.ros_fp if sort == "ros" else r.projection.fp_per_gp, reverse=True)
    what = " ".join(
        x
        for x in [
            pos.upper() if pos else "All players",
            "available" if available else "",
            f"on {owner}" if owner else "",
        ]
        if x
    )
    console.print(
        rank_table(
            f"{what}: ranked by {'rest-of-season FP' if sort == 'ros' else 'FP/GP'}",
            rows[:limit],
            rules.source,
        )
    )


@app.command()
def player(name: str = typer.Argument(..., help="Player name (or part of it), or a Fantrax id.")) -> None:
    """Show every component behind one player's projection."""
    settings, conn, http = _open()
    rules = load_rules(conn)
    matches = Valuer(conn, rules, settings.league).find(name)
    if not matches:
        console.print(
            f"[red]No player matching {name!r}.[/] Names come from Fantrax; try part of the last name."
        )
        raise typer.Exit(1)
    if len(matches) > 1:
        t = Table(title=f"{len(matches)} players match {name!r}; be more specific", title_justify="left")
        for c in ("Player", "Pos", "NHL", "Owner", "FX id"):
            t.add_column(c)
        for r in matches[:20]:
            t.add_row(r.name, r.positions, r.nhl_team or "", r.owner or "", r.fantrax_id)
        console.print(t)
        console.print("Pass the FX id to pick one, e.g. `hockey player 03rmx`.")
        raise typer.Exit(1)
    for part in player_tables(matches[0], rules):
        console.print(part)


def _fail(e: Exception, code: int = 1):
    console.print(f"[red]{e}[/]")
    raise typer.Exit(code) from e


def _context(team: str | None = None, refresh: bool = False):
    """Settings, db, http and my team as a LeagueContext; exits with a message if the team is unknown."""
    from hockey.context import ContextError, LeagueContext, my_team

    settings, conn, http = _open(refresh)
    try:
        return LeagueContext(settings, conn, http, my_team(conn, settings, team))
    except ContextError as e:
        _fail(e)


def _team_period(team: str | None, period: int | None, current: bool = False):
    """LeagueContext (http still open: the caller closes it), the chosen period, its games, now."""
    from datetime import datetime

    from hockey.context import ContextError, choose_period

    ctx = _context(team)
    now = datetime.now(UTC)
    try:
        chosen = choose_period(ctx.conn, period, current, now)
        games = ctx.games(chosen)
    except ContextError as e:
        ctx.http.close()
        _fail(e)
    except FetchError as e:
        ctx.http.close()
        console.print(f"[bold red]Couldn't load the NHL schedule:[/] {e}")
        raise typer.Exit(1) from e
    return ctx, chosen, games, now


def _team_period_games(team: str | None, period: int | None, current: bool = False):
    """Shared by lineup/waivers: my team row, the chosen roster period, and its NHL games."""
    from datetime import datetime

    from hockey.context import ContextError, choose_period

    ctx = _context(team)
    now = datetime.now(UTC)
    try:
        chosen = choose_period(ctx.conn, period, current, now)
        games = ctx.games(chosen)
    except ContextError as e:
        _fail(e)
    except FetchError as e:
        console.print(f"[bold red]Couldn't load the NHL schedule:[/] {e}")
        raise typer.Exit(1) from e
    finally:
        ctx.http.close()
    return ctx.settings, ctx.conn, ctx.me, chosen, games, now


@app.command()
def lineup(
    period: int = typer.Option(None, help="Roster period number (default: the next lineup lock)."),
    current: bool = typer.Option(False, "--current", help="Show the period in progress (already locked)."),
    out: list[str] = typer.Option(None, "--out", help="A player who won't play this period (repeatable)."),
    team: str = typer.Option(None, help="Team name or short name (default: MY_TEAM_NAME)."),
    ai: bool = typer.Option(False, "--ai", help="Use AI news flags: players reported out are benched."),
    play: list[str] = typer.Option(
        None, "--play", help="With --ai: ignore an AI 'out' flag for this player (repeatable)."
    ),
) -> None:
    """Recommend the best lineup for a weekly lock: games scheduled, goalie starts, availability."""
    from hockey.lineup.report import build_report
    from hockey.views.tables import lineup_tables, news_table

    settings, conn, row, chosen, games, now = _team_period_games(team, period, current)
    news = _news(conn, settings) if ai else None
    news_outs = _news_outs(news, play)
    league_roster = settings.league.get("roster") or {}
    report = build_report(
        Valuer(conn, load_rules(conn), settings.league),
        row["team_id"],
        chosen,
        games,
        out or [],
        slots=league_roster.get("active") or None,
        reserve_max=league_roster.get("reserve", 6),
        ir_max=league_roster.get("injured_reserve", 3),
        news_outs=news_outs,
    )
    if not games:
        console.print(f"[yellow]No regular-season NHL games in period {chosen.number}.[/]")
    for part in lineup_tables(row["name"], report, now):
        console.print(part)
    if news:
        names = {w.row.name for w in report.starters + report.bench + report.others}
        t = news_table(news, names, "AI news for your roster (judgment from news, not the engine)")
        console.print(t or "[dim]AI news: nothing reported for your roster.[/]")
        if news_outs:
            console.print(
                f"[bold]Benched as out per AI news:[/] {', '.join(news_outs)}. "
                "Override with `--play NAME` if you know better."
            )


@app.command()
def waivers(
    pos: str = typer.Option(None, help="Only this slot: C, LW, RW, D or G."),
    limit: int = typer.Option(10, help="Rows per table."),
    protect: list[str] = typer.Option(
        None, "--protect", help="Never suggest dropping this player (repeatable)."
    ),
    period: int = typer.Option(None, help="Roster period for the weekly numbers (default: the next lock)."),
    max_ros_cost: float = typer.Option(
        10.0, help="Streamers: most a drop may cost (rest-of-season points + weighted keeper value)."
    ),
    keeper_weight: float = typer.Option(
        0.5, help="Weight of a dropped player's keeper value vs this season's points (as in `trade`)."
    ),
    team: str = typer.Option(None, help="Team name or short name (default: MY_TEAM_NAME)."),
    ai: bool = typer.Option(False, "--ai", help="Show AI news flags for the players in this report."),
) -> None:
    """Best free-agent pickups vs your weakest players, and streamers for the coming week.

    Drops are priced with keeper value too (what your best keeper set loses without the player)."""
    from hockey.views.tables import waiver_tables

    if pos and pos.upper() not in ("C", "LW", "RW", "D", "G"):
        console.print("[red]--pos must be one of C, LW, RW, D, G[/]")
        raise typer.Exit(2)
    ctx, chosen, games, now = _team_period(team, period)
    try:
        report = ctx.waivers(
            chosen,
            protect=protect or [],
            pos=pos.upper() if pos else None,
            limit=limit,
            max_ros_cost=max_ros_cost,
            keeper_weight=keeper_weight,
        )
    finally:
        ctx.http.close()
    settings, conn, row = ctx.settings, ctx.conn, ctx.me
    for part in waiver_tables(row["name"], report):
        console.print(part)
    if ai:
        from hockey.views.tables import news_table

        news = _news(conn, settings)
        opts = report.pickups + report.streamers + [p.best for p in report.by_position if p.best]
        names = {o.pickup.row.name for o in opts} | {o.drop.row.name for o in opts if o.drop}
        names |= {p.row.name for p in report.depth}
        t = news_table(news, names, "AI news for these players (judgment from news, not the engine)")
        console.print(t or "[dim]AI news: nothing reported for the players above.[/]")


def _resolve(names: str, rows, where: str):
    """Comma-separated names (or Fantrax ids) -> RosterRows from ``rows``; exits on unknown/ambiguous."""
    from hockey.context import ContextError, resolve

    try:
        return resolve(names, rows, where)
    except ContextError as e:
        _fail(e)


@app.command()
def trade(
    give: str = typer.Argument(
        ..., help='Players you send, comma-separated ("-" for none), e.g. "Aho, Tuch".'
    ),
    get: str = typer.Argument(..., help='Players you receive, comma-separated ("-" for none).'),
    give_pick: list[str] = typer.Option(
        None, "--give-pick", help="Draft pick you send, e.g. 2027:2 (repeatable)."
    ),
    get_pick: list[str] = typer.Option(
        None, "--get-pick", help="Draft pick you receive, e.g. 2027:1 (repeatable)."
    ),
    partner: str = typer.Option(None, help="Other team (needed only when you receive no players)."),
    keeper_weight: float = typer.Option(
        0.5, help="Weight of next-season keeper + pick value vs this season."
    ),
    team: str = typer.Option(None, help="Your team (default: MY_TEAM_NAME)."),
) -> None:
    """Evaluate a proposed trade for both sides: roster fit, scarcity, keeper and pick value."""
    from hockey.context import ContextError
    from hockey.views.tables import trade_tables

    ctx = _context(team)
    try:
        report = ctx.trade(
            give,
            get,
            give_picks=give_pick,
            get_picks=get_pick,
            partner=partner,
            keeper_weight=keeper_weight,
        )
    except ContextError as e:
        _fail(e)
    for part in trade_tables(report):
        console.print(part)


@app.command()
def keepers(
    horizon: int = typer.Option(3, help="Seasons of future value to count."),
    keepers_file: Path = typer.Option(None, help="Keeper clock file (default: data/keepers.yaml)."),
    team: str = typer.Option(None, help="Team name or short name (default: MY_TEAM_NAME)."),
) -> None:
    """Rank your players by multi-year keeper value and pick the best 10 + 5 under the league rules."""
    from hockey.views.tables import keeper_tables

    ctx = _context(team)
    try:
        result = ctx.keeper_plan(horizon, keepers_file)
    except FetchError as e:
        console.print(f"[bold red]Couldn't load NHL career data:[/] {e}")
        raise typer.Exit(1) from e
    finally:
        ctx.http.close()
    for part in keeper_tables(ctx.me["name"], result, horizon):
        console.print(part)


def _league_intel(conn, settings, me):
    from hockey.context import league_intel

    return league_intel(conn, settings, me)


@app.command()
def intel(team: str = typer.Option(None, help="Team name or short name (default: MY_TEAM_NAME).")) -> None:
    """Every team's strengths and weaknesses, and the best trade partners for your needs."""
    from hockey.views.tables import intel_tables

    settings, conn, http = _open()
    if team:
        settings.my_team_name, settings.my_team_short = team, team
    me = find_my_team(conn, settings)
    if me is None:
        console.print(f"[red]Team {settings.my_team_name!r} not found.[/] Run `hockey sync` first.")
        raise typer.Exit(1)
    league, period = _league_intel(conn, settings, me)
    for part in intel_tables(league, period):
        console.print(part)


@app.command()
def report(
    do_sync: bool = typer.Option(False, "--sync", help="Refresh all data first (same as `hockey sync`)."),
    out: list[str] = typer.Option(None, "--out", help="A player who won't play this period (repeatable)."),
    out_dir: Path = typer.Option(None, help="Where to write the report (default: var/reports/)."),
    ai: bool = typer.Option(False, "--ai", help="Add an AI take: news flags + Claude's read of the report."),
) -> None:
    """Write today's markdown report: lineup moves, pickups, streamers and league intel."""
    from datetime import datetime

    from hockey.report.daily import render, write

    if do_sync:
        sync(refresh=False, skip_nhl=False, skip_moneypuck=False)
    ctx, chosen, games, now = _team_period(None, None)
    settings, conn, row = ctx.settings, ctx.conn, ctx.me
    errors = []
    news = _news(conn, settings) if ai else None
    try:
        lineup_r = ctx.lineup(chosen, out or [], _news_outs(news, None))
        waivers_r = ctx.waivers(chosen)
    finally:
        ctx.http.close()
    league_r, _ = _league_intel(conn, settings, row)
    if not games:
        errors.append(f"No regular-season NHL games in period {chosen.number}.")
    sections = {"lineup": lineup_r, "waivers": waivers_r, "league": league_r, "errors": errors}
    md = render(row["name"], datetime.now(UTC), **sections)
    if ai:
        md = render(
            row["name"],
            datetime.now(UTC),
            **sections,
            ai_take=_report_take(conn, settings, row, md, news),
            news=news,
        )
    path = write(md, out_dir or REPO_ROOT / "var" / "reports", now)
    console.print(f"[green]Report written:[/] {path}")
    console.print(
        f"Lineup: {lineup_r.gain:+.1f} FP available · pickups: {len(waivers_r.pickups)} · "
        f"streamers: {len(waivers_r.streamers)} · trade ideas: {sum(len(s) for _, s in league_r.partners)}"
    )


def _ai_client(settings, **overrides):
    """The AI client, or a one-line note and a clean exit when the AI layer is off."""
    from hockey.ai.client import AiUnavailable, make_client

    try:
        return make_client(settings.league, **overrides)
    except AiUnavailable as e:
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(0) from e


def _on_tool(name: str, args: dict) -> None:
    detail = args.get("query") if name == "web_search" else ", ".join(f"{k}={v}" for k, v in args.items())
    console.print(f"[dim]  → {name}({detail or ''})[/]")


def _news(conn, settings, refresh: bool = False, required: bool = False):
    """Fresh stored AI news flags, or a new news scan when they're stale.

    Without the AI layer: ``required`` exits with the one-line fix; otherwise a note, and None (the
    command carries on with the numbers alone).
    """
    from hockey.ai.client import AiError, AiSettings, AiUnavailable, make_client
    from hockey.context import ContextError, LeagueContext, my_team

    try:
        from hockey.ai import news as ai_news  # needs pydantic (the `ai` extra)
    except ImportError:
        console.print("[yellow]AI layer off: install it with `uv sync --extra ai`[/]")
        if required:
            raise typer.Exit(0) from None
        return None
    ttl = AiSettings.from_league(settings.league).news_ttl_hours
    cached = None if refresh else ai_news.fresh(conn, ttl)
    if cached:
        return cached
    try:
        client = make_client(settings.league)
        console.print("[dim]Checking the news for your roster and the top free agents (web search)…[/]")
        ctx = LeagueContext(settings, conn, None, my_team(conn, settings))
        try:
            return ai_news.scan(ctx, client, on_tool=_on_tool)
        finally:
            _dropped_note(client)
    except AiUnavailable as e:
        console.print(f"[yellow]{e}[/]" + ("" if required else " (continuing without AI news)"))
        if required:
            raise typer.Exit(0) from e
    except (AiError, ContextError) as e:
        if required:
            _fail(e)
        console.print(f"[yellow]AI news skipped: {e}[/]")
    return None


def _dropped_note(client) -> None:
    if client.dropped_domains:
        console.print(
            f"[yellow]Skipped news sites that block Anthropic's search: {', '.join(client.dropped_domains)}. "
            "Remove them from ai.news_domains in data/league.yaml.[/]"
        )


def _news_outs(news, play: list[str] | None) -> dict[str, str]:
    if not news:
        return {}
    from hockey.ai.news import outs

    return {f["player"]: f["note"] for f in outs(news, play)}


def _report_take(conn, settings, me, markdown: str, news) -> str | None:
    from hockey.ai.client import AiError, AiUnavailable, make_client
    from hockey.context import LeagueContext

    try:
        from hockey.ai.take import report_take

        ans = report_take(
            LeagueContext(settings, conn, None, me), make_client(settings.league), markdown, news
        )
    except (AiError, AiUnavailable, ImportError) as e:
        console.print(f"[yellow]AI take skipped: {e}[/]")
        return None
    return ans.text


@app.command()
def ask(
    question: str = typer.Argument(None, help="Your question. Leave it out for a back-and-forth chat."),
    web: bool = typer.Option(True, "--web/--no-web", help="Let Claude search recent news (injuries, lines)."),
    model: str = typer.Option(None, help="Claude model (default: ai.model in data/league.yaml)."),
    effort: str = typer.Option(None, help="low / medium / high / xhigh / max (default: ai.effort)."),
    team: str = typer.Option(None, help="Your team (default: MY_TEAM_NAME)."),
) -> None:
    """Ask the AI assistant: Claude uses the engine's numbers (via read-only tools) plus news and judgment."""
    from hockey.ai.agent import converse
    from hockey.ai.client import AiError
    from hockey.ai.prompts import system_blocks
    from hockey.ai.tools import Executor
    from hockey.sources.moneypuck import CREDIT

    ctx = _context(team)
    client = _ai_client(ctx.settings, model=model, effort=effort, web_search=False if not web else None)
    use_web = web and client.settings.web_search
    system = system_blocks(
        ctx.league,
        ctx.valuer.rules,
        f"The manager's team is {ctx.me['name']}.",
    )
    executor = Executor(ctx)
    messages: list[dict] = []
    chat = question is None
    if chat:
        console.print("[dim]Ask about your lineup, pickups, trades or keepers. Empty line to quit.[/]")
    try:
        while True:
            q = question if not chat else typer.prompt("\nYou", default="", show_default=False)
            if not q.strip():
                break
            if not messages:  # the date goes in the first turn, not the (cached) system prompt
                q = f"(Today is {date.today():%A %Y-%m-%d}.)\n\n{q}"
            messages.append({"role": "user", "content": q})
            try:
                ans = converse(
                    client,
                    system,
                    messages,
                    executor,
                    web=use_web,
                    on_text=lambda t: console.print(t, end="", markup=False, highlight=False, soft_wrap=True),
                    on_tool=_on_tool,
                )
            except AiError as e:
                _fail(e)
            finally:
                _dropped_note(client)
            console.print()
            if ans.truncated:
                console.print("[yellow]The answer hit the length limit and was cut short.[/]")
            if ans.sources:
                console.print("[bold]Sources:[/]")
                for title, url in ans.sources[:10]:
                    console.print(f"  • {title}: {url}", markup=False, highlight=False)
            if executor.uses_moneypuck:
                console.print(f"[dim]{CREDIT}[/]")
            console.print(
                f"[dim]Recommendations only: make any moves yourself on Fantrax. "
                f"({ans.input_tokens + ans.cache_read_tokens:,} in / {ans.output_tokens:,} out tokens"
                f"{f', {ans.searches} searches' if ans.searches else ''})[/]"
            )
            if not chat:
                break
    finally:
        ctx.http.close()


@app.command()
def news(
    refresh: bool = typer.Option(False, "--refresh", help="Search again even if the stored flags are fresh."),
    mine: bool = typer.Option(False, "--mine", help="Only players on your roster."),
) -> None:
    """AI news scan: injuries, line/PP changes and goalie starts for your roster and top free agents."""
    from hockey.context import LeagueContext, my_team
    from hockey.views.tables import news_table

    settings, conn, http = _open()
    http.close()
    result = _news(conn, settings, refresh, required=True)
    only = None
    if mine:
        from hockey.ai.news import watchlist

        ctx = LeagueContext(settings, conn, None, my_team(conn, settings))
        only = {r.name for r in watchlist(ctx, free_agents=0)}
    t = news_table(result, only)
    console.print(t or "[dim]No news flags: nothing notable found for the players checked.[/]")
    if result.searches:
        console.print(f"[dim]{result.searches} web searches.[/]")
    console.print(
        "[dim]Use them: `hockey lineup --ai` benches players reported out; `hockey waivers --ai` and "
        "`hockey report --ai` show them. Recommendations only: make any moves yourself on Fantrax.[/]"
    )


@app.command()
def web(
    port: int = typer.Option(8765, help="Port on 127.0.0.1."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the page in your browser."),
) -> None:
    """Browser UI on http://127.0.0.1:PORT (this machine only). Same engine as the CLI; Ctrl+C to stop."""
    try:
        from hockey.web.app import serve
    except ImportError as e:
        console.print("[yellow]Browser UI off: install it with `uv sync --extra web`[/]")
        raise typer.Exit(0) from e
    console.print(f"Hockey assistant running at [bold]http://127.0.0.1:{port}/[/] (Ctrl+C to stop)")
    serve(port, open_browser)


@app.command("validate-scoring")
def validate_scoring(
    path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Fantrax Players CSV with stats + FPts."
    ),
    tolerance: float = typer.Option(0.1, help="Allowed |computed - Fantrax| per player."),
) -> None:
    """Re-score a Fantrax stats export with our engine and compare with Fantrax's own FPts."""
    from hockey.scoring_check import check_csv

    settings, conn, http = _open()
    rules = load_rules(conn)
    if rules.is_empty():
        console.print("[red]No scoring rules loaded; run `hockey sync` first.[/]")
        raise typer.Exit(1)
    result = check_csv(path, rules, tolerance)
    console.print(kv_table("Scoring validation", result.summary()))
    if result.missing_columns:
        console.print(
            f"[yellow]Scored stats not in the CSV (add those columns in the Fantrax stats view): "
            f"{result.missing_columns}[/]"
        )
    if result.worst:
        t = Table(title="Largest differences", title_justify="left")
        for c in ("Player", "Pos", "Fantrax FPts", "Computed", "Diff"):
            t.add_column(c)
        for name, pos, fpts, comp in result.worst:
            t.add_row(name, pos, f"{fpts:.1f}", f"{comp:.1f}", f"{comp - fpts:+.1f}")
        console.print(t)
    raise typer.Exit(0 if result.ok else 1)


if __name__ == "__main__":
    app()
