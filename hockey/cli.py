"""`hockey` CLI. Read-only: prints recommendations, never acts on Fantrax."""

from __future__ import annotations

import json
import logging
from datetime import date
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
        False, "--all", help="With --unmatched: include non-rostered/non-pool players."
    ),
) -> None:
    """ID-mapping health: counts by method, unmatched and fuzzy lists."""
    settings, conn, http = _open()
    counts = {
        r["method"]: r["n"] for r in conn.execute("SELECT method, COUNT(*) n FROM player_map GROUP BY method")
    }
    counts["unmatched (rostered/pool)"] = conn.execute(
        "SELECT COUNT(*) FROM id_unmatched WHERE relevant=1"
    ).fetchone()[0]
    console.print(kv_table("Fantrax → NHL id mapping", counts))
    if unmatched:
        t = Table(title="Unmatched", title_justify="left")
        for c in ("FX id", "Name", "Team", "Pos", "Reason", "Nearest candidates"):
            t.add_column(c)
        q = "SELECT * FROM id_unmatched" + ("" if all_players else " WHERE relevant=1") + " ORDER BY name"
        for r in conn.execute(q):
            cands = "; ".join(f"{i}: {n}" for i, n in json.loads(r["candidates"] or "[]"))
            t.add_row(
                r["fantrax_id"], r["name"], r["nhl_team"] or "", r["pos_group"] or "", r["reason"], cands
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
