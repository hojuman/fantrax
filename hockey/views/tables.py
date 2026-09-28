"""Rich terminal tables."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from hockey.projection.inseason import ProjectionV2
from hockey.scoring.rules import ScoringRules
from hockey.sources.moneypuck import CREDIT
from hockey.valuation import RosterRow

STAT_LABELS = {
    "g": "G",
    "a": "A",
    "pts": "PTS",
    "pm": "+/-",
    "pim": "PIM",
    "ppg": "PPG",
    "ppa": "PPA",
    "ppp": "PPP",
    "shg": "SHG",
    "sha": "SHA",
    "shp": "SHP",
    "gwg": "GWG",
    "otg": "OTG",
    "sog": "SOG",
    "hit": "HIT",
    "blk": "BLK",
    "fow": "FOW",
    "fol": "FOL",
    "tk": "TK",
    "gv": "GV",
    "toi_min": "TOI",
    "evg": "EVG",
    "evp": "EVP",
    "gp": "GP",
    "gs": "GS",
    "w": "W",
    "l": "L",
    "otl": "OTL",
    "ga": "GA",
    "sv": "SV",
    "sa": "SA",
    "so": "SO",
    "sv_pct": "SV%",
}
STATUS_ORDER = {
    "ACTIVE": 0,
    "ACT": 0,
    "RESERVE": 1,
    "RES": 1,
    "MINORS": 2,
    "MIN": 2,
    "INJURED_RESERVE": 3,
    "IR": 3,
}
POS_ORDER = {"F": 0, "D": 1, "G": 2}
MATCH_FLAG = {
    None: "[red]✗ unmatched[/]",
    "fuzzy": "[yellow]~ fuzzy[/]",
}


def _match_flag(method: str | None) -> str:
    return MATCH_FLAG.get(method, "[green]✓[/]")


def _components(row: RosterRow, n: int = 3) -> str:
    if not row.projection:
        return ""
    return "  ".join(f"{STAT_LABELS.get(s, s)} {v:+.2f}" for s, v in row.projection.breakdown.top(n))


def basis(p: ProjectionV2 | None) -> str:
    """Short description of what a projection is built on."""
    if p is None:
        return ""
    if p.method == "rookie default":
        return "[yellow]rookie est.[/]"
    now = p.season.gp if p.season else 0
    if p.prior is None:
        return f"{now} GP now · rookie prior"
    if now:
        return f"{p.prior.sample_gp} GP prior + {now} now"
    return f"{p.prior.sample_gp} GP · {p.prior.shrink:.0%} reg"


def _caption(scoring_source: str, rows: list[RosterRow]) -> str:
    caption = (
        f"scoring: {scoring_source} · prior = 3 seasons regressed to position mean, "
        "updated with this season (recent games weighted up)"
    )
    if any(r.projection and r.projection.uses_moneypuck for r in rows):
        caption += f"\n{CREDIT}"
    return caption


def roster_table(team_name: str, rows: list[RosterRow], scoring_source: str) -> Table:
    def key(r: RosterRow):
        st = STATUS_ORDER.get((r.status or "").upper(), 1)
        fp = r.projection.ros_fp if r.projection else -1e9
        return (st, POS_ORDER.get(r.pos_group or "", 3), -fp)

    t = Table(
        title=f"{team_name}: projected fantasy value",
        caption=_caption(scoring_source, rows),
        title_justify="left",
    )
    for col, just in [
        ("Status", "left"),
        ("Player", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("ROS GP", "right"),
        ("FP/GP", "right"),
        ("ROS FP", "right"),
        ("Top components (FP/GP)", "left"),
        ("Basis", "right"),
        ("ID", "left"),
        ("FX id", "left"),
    ]:
        t.add_column(col, justify=just, no_wrap=col not in ("Top components (FP/GP)", "Basis"))
    total = 0.0
    for r in sorted(rows, key=key):
        p = r.projection
        if p:
            total += p.ros_fp
        t.add_row(
            r.status or r.slot or "",
            r.name,
            r.positions,
            r.nhl_team or "",
            f"{p.ros_gp:.0f}" if p else "–",
            f"{p.fp_per_gp:.2f}" if p else "–",
            f"{p.ros_fp:.0f}" if p else "–",
            _components(r),
            basis(p),
            _match_flag(r.match_method),
            r.fantrax_id,
        )
    t.add_section()
    t.add_row("", "[bold]Total[/]", "", "", "", "", f"[bold]{total:.0f}[/]", "", "", "", "")
    return t


def rank_table(title: str, rows: list[RosterRow], scoring_source: str, sort: str = "fp_per_gp") -> Table:
    t = Table(title=title, caption=_caption(scoring_source, rows), title_justify="left")
    for col, just in [
        ("#", "right"),
        ("Player", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("Owner", "left"),
        ("FP/GP", "right"),
        ("ROS GP", "right"),
        ("ROS FP", "right"),
        ("Top components (FP/GP)", "left"),
        ("Basis", "right"),
    ]:
        t.add_column(col, justify=just, no_wrap=col not in ("Top components (FP/GP)", "Basis", "Owner"))
    for i, r in enumerate(rows, 1):
        p = r.projection
        owner = r.owner or ""
        owner = f"[green]{owner}[/]" if owner in ("FA", "W") else owner
        t.add_row(
            str(i),
            r.name,
            r.positions,
            r.nhl_team or "",
            owner,
            f"{p.fp_per_gp:.2f}" if p else "–",
            f"{p.ros_gp:.0f}" if p else "–",
            f"{p.ros_fp:.0f}" if p else "–",
            _components(r),
            basis(p),
        )
    return t


def _rate(x: float | None, stat: str) -> str:
    if x is None:
        return "–"
    return f"{x:.3f}" if stat == "sv_pct" else f"{x:.2f}"


def player_tables(r: RosterRow, rules: ScoringRules) -> list:
    """Everything behind one player's projection, component by component."""
    p = r.projection
    head = kv_table(
        f"{r.name}",
        {
            "Position / NHL team": f"{r.positions or r.pos_group} · {r.nhl_team or 'no team'}",
            "Fantasy owner": r.owner or "",
            "NHL id match": f"{r.nhl_id or '–'} ({r.match_method or 'unmatched'})",
            "Projection method": p.method if p else "none",
        },
    )
    if p is None:
        return [head]
    weights = rules.for_pos_group(p.pos_group)
    stats = [s for s in weights if weights[s]]
    extra = ["toi_min"] + (["sa", "sv_pct"] if p.pos_group == "G" else [])
    t = Table(
        title="Per-game rates: prior → this season → recent → projection",
        title_justify="left",
        caption="Prior wt = share of the projection still coming from the prior (falls as games accumulate)",
    )
    sgp = f" ({p.season.gp} GP)" if p.season else ""
    l30 = f" ({p.last30.gp})" if p.last30 else ""
    l14 = f" ({p.last14.gp})" if p.last14 else ""
    for col in (
        "Stat",
        "Pts each",
        "Prior",
        f"Season{sgp}",
        f"Last 30{l30}",
        f"Last 14{l14}",
        "Projection",
        "Prior wt",
        "FP/GP",
    ):
        t.add_column(col, justify="left" if col == "Stat" else "right")

    def window_rate(w, stat):
        if w is None or not w.gp:
            return None
        if stat == "sv_pct":
            return w.stats.get("sv", 0.0) / w.stats["sa"] if w.stats.get("sa") else None
        return w.rate(stat)

    for stat in stats + [s for s in extra if s not in stats]:
        prior = p.prior_rates.get(stat)
        if stat == "sv_pct" and p.prior_rates.get("sa"):
            prior = p.prior_rates.get("sv", 0.0) / p.prior_rates["sa"]
        scored = stat in stats
        t.add_row(
            STAT_LABELS.get(stat, stat) + ("" if scored else " [dim](context)[/]"),
            f"{weights[stat]:g}" if scored else "",
            _rate(prior, stat),
            _rate(window_rate(p.season, stat), stat),
            _rate(window_rate(p.last30, stat), stat),
            _rate(window_rate(p.last14, stat), stat),
            _rate(p.rates.get(stat), stat),
            f"{p.prior_weight[stat]:.0%}" if stat in p.prior_weight else "",
            f"{p.breakdown.parts.get(stat, 0.0):+.2f}" if scored else "",
        )
    t.add_section()
    t.add_row("[bold]Total[/]", "", "", "", "", "", "", "", f"[bold]{p.fp_per_gp:.2f}[/]")

    summary = {
        "Fantasy points per game": f"{p.fp_per_gp:.2f}",
        "Share of team games": f"{p.games_share:.0%}",
        "Rest-of-season games": f"{p.ros_gp:.0f}",
        "Rest-of-season points": f"{p.ros_fp:.0f}",
        "Basis": basis(p),
    }
    out = [head, t, kv_table("Projection", summary)]
    if p.notes:
        out.append("\n".join(f"[cyan]•[/] {n}" for n in p.notes))
    if p.uses_moneypuck:
        out.append(f"[dim]{CREDIT}[/]")
    return out


def kv_table(title: str, data: dict) -> Table:
    t = Table(title=title, title_justify="left", show_header=False)
    t.add_column(style="bold")
    t.add_column(justify="right")
    for k, v in data.items():
        t.add_row(str(k), str(v))
    return t


console = Console()
