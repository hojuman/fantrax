"""Rich terminal tables."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

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


def roster_table(team_name: str, rows: list[RosterRow], scoring_source: str) -> Table:
    def key(r: RosterRow):
        st = STATUS_ORDER.get((r.status or "").upper(), 1)
        fp = r.projection.fp_season if r.projection else -1e9
        return (st, POS_ORDER.get(r.pos_group or "", 3), -fp)

    t = Table(
        title=f"{team_name}: projected fantasy value",
        caption=f"scoring: {scoring_source} · "
        "baseline = 3-season weighted per-GP rates, regressed to position mean",
        title_justify="left",
    )
    for col, just in [
        ("Status", "left"),
        ("Player", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("Proj GP", "right"),
        ("FP/GP", "right"),
        ("Proj FP", "right"),
        ("Top components (FP/GP)", "left"),
        ("Sample", "right"),
        ("ID", "left"),
        ("FX id", "left"),
    ]:
        t.add_column(col, justify=just, no_wrap=col not in ("Top components (FP/GP)",))
    total = 0.0
    for r in sorted(rows, key=key):
        p = r.projection
        if p:
            total += p.fp_season
        sample = f"{p.sample_gp} GP · {p.shrink:.0%} reg" if p else ("no NHL stats" if r.nhl_id else "")
        t.add_row(
            r.status or r.slot or "",
            r.name,
            r.positions,
            r.nhl_team or "",
            f"{p.proj_gp:.0f}" if p else "–",
            f"{p.fp_per_gp:.2f}" if p else "–",
            f"{p.fp_season:.0f}" if p else "–",
            _components(r),
            sample,
            _match_flag(r.match_method),
            r.fantrax_id,
        )
    t.add_section()
    t.add_row("", "[bold]Total[/]", "", "", "", "", f"[bold]{total:.0f}[/]", "", "", "", "")
    return t


def kv_table(title: str, data: dict) -> Table:
    t = Table(title=title, title_justify="left", show_header=False)
    t.add_column(style="bold")
    t.add_column(justify="right")
    for k, v in data.items():
        t.add_row(str(k), str(v))
    return t


console = Console()
