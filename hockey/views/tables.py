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


def _games_cell(w) -> str:
    cell = str(w.avail.games)
    if w.row.pos_group == "G" and w.avail.games:
        cell += f" ({w.avail.expected:.1f} st)"
    if w.avail.b2b:
        cell += " b2b" if w.avail.b2b == 1 else f" {w.avail.b2b}×b2b"
    return cell


def lineup_tables(team_name: str, r, now) -> list:
    """Renderables for a LineupReport (hockey/lineup/report.py)."""
    local_lock = r.period.start.astimezone()
    hours = (r.period.start - now).total_seconds() / 3600
    when = f"locks in {hours:.0f} h" if hours > 0 else "already locked (in progress)"
    head = (
        f"[bold]{team_name}: lineup for period {r.period.number}[/] · "
        f"{r.period.start.astimezone():%a %b %d} → {r.period.end.astimezone():%a %b %d} · "
        f"lock {local_lock:%a %b %d %H:%M %Z} ({when}) · {r.games_in_period} NHL games"
    )
    out: list = [head]

    t = Table(title="Recommended actives", title_justify="left")
    for col, just in [
        ("Slot", "left"),
        ("Player", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("Games", "right"),
        ("FP/GP", "right"),
        ("Exp FP", "right"),
        ("Change", "left"),
    ]:
        t.add_column(col, justify=just, no_wrap=True)
    for w in r.starters:
        change = "stays" if w.status == "ACTIVE" else f"[green]↑ from {w.status.title() or 'bench'}[/]"
        t.add_row(
            w.slot,
            w.row.name,
            w.row.positions,
            w.row.nhl_team or "",
            _games_cell(w),
            f"{w.fp_per_gp:.2f}",
            f"{w.value:.1f}",
            change,
        )
    for pos, n in r.empty.items():
        for _ in range(n):
            t.add_row(pos, "[red](empty)[/]", "", "", "", "", "", "")
    t.add_section()
    t.add_row("", "[bold]Total[/]", "", "", "", "", f"[bold]{r.total:.1f}[/]", "")
    out.append(t)

    if r.bench or r.others:
        b = Table(title="Bench, IR and Minors", title_justify="left")
        for col, just in [
            ("Status", "left"),
            ("Player", "left"),
            ("Pos", "left"),
            ("NHL", "left"),
            ("Games", "right"),
            ("Exp FP", "right"),
            ("Why not starting", "left"),
        ]:
            b.add_column(col, justify=just, no_wrap=col != "Why not starting")
        for w in r.bench + r.others:
            if w.avail.out:
                why = w.avail.flags[0]
            elif w.status in ("INJURED_RESERVE", "MINORS"):
                why = "IR slot" if w.status == "INJURED_RESERVE" else "Minors slot"
            elif w.avail.games == 0:
                why = "no games"
            else:
                why = "lower projected value"
            b.add_row(
                w.status.title().replace("_", " ") or "?",
                w.row.name,
                w.row.positions,
                w.row.nhl_team or "",
                _games_cell(w),
                f"{w.value:.1f}",
                why,
            )
        out.append(b)

    if r.moves_in or r.moves_out:
        lines = ["[bold]Moves to make on Fantrax[/] (this tool never makes them for you):"]
        lines += [f"  • Move [green]{w.row.name}[/] to Active ({w.slot})" for w in r.moves_in]
        lines += [f"  • Move [yellow]{w.row.name}[/] to Reserve" for w in r.moves_out]
        out.append("\n".join(lines))
    else:
        out.append("[green]Your current lineup is already optimal.[/]")
    out.append(
        f"Projected: current lineup {r.current_total:.1f} FP → recommended {r.total:.1f} FP "
        f"([bold]{r.gain:+.1f}[/])"
    )
    if r.flags:
        out.append("\n".join(f"[yellow]![/] {f}" for f in r.flags))
    if r.uses_moneypuck:
        out.append(f"[dim]{CREDIT}[/]")
    return out


def _status(owner: str | None) -> str:
    return "[yellow]W[/]" if owner == "W" else "FA"


def waiver_tables(team_name: str, r) -> list:
    """Renderables for a WaiverReport (hockey/waivers/report.py)."""
    out: list = [
        f"[bold]{team_name}: waiver report[/] · {r.open_spots} open roster spot{'' if r.open_spots == 1 else 's'} · "
        f"weekly numbers for period {r.period.number} ({r.period.start.astimezone():%a %b %d} → "
        f"{r.period.end.astimezone():%a %b %d})"
    ]

    t = Table(
        title="Best pickups for the rest of the season (gain = better starting lineup)", title_justify="left"
    )
    for col, just in [
        ("Pick up", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("", "left"),
        ("FP/GP", "right"),
        ("ROS FP", "right"),
        ("Drop", "left"),
        ("ROS gain", "right"),
        ("Games wk", "right"),
    ]:
        t.add_column(col, justify=just, no_wrap=True)
    for o in r.pickups:
        p = o.pickup
        t.add_row(
            p.row.name,
            p.row.positions,
            p.row.nhl_team or "",
            _status(p.row.owner),
            f"{p.row.projection.fp_per_gp:.2f}",
            f"{p.ros:.0f}",
            o.drop.row.name if o.drop else "[green](open spot)[/]",
            f"[bold]{o.gain:+.1f}[/]",
            str(p.avail.games),
        )
    if not r.pickups:
        t.add_row("[dim]No free agent improves your starting lineup[/]", *[""] * 8)
    out.append(t)

    if r.depth:
        d = Table(
            title="Best use of your open spot: depth for injuries (or stream it week to week, below)",
            title_justify="left",
        )
        for col, just in [
            ("Add", "left"),
            ("Pos", "left"),
            ("NHL", "left"),
            ("", "left"),
            ("FP/GP", "right"),
            ("ROS FP", "right"),
            ("Games wk", "right"),
        ]:
            d.add_column(col, justify=just, no_wrap=True)
        for p in r.depth:
            d.add_row(
                p.row.name,
                p.row.positions,
                p.row.nhl_team or "",
                _status(p.row.owner),
                f"{p.row.projection.fp_per_gp:.2f}",
                f"{p.ros:.0f}",
                str(p.avail.games),
            )
        out.append(d)

    b = Table(title="By position: your weakest starter vs the best available", title_justify="left")
    for col, just in [
        ("Slot", "left"),
        ("Your weakest", "left"),
        ("ROS FP", "right"),
        ("Best available", "left"),
        ("ROS FP", "right"),
        ("ROS gain", "right"),
    ]:
        b.add_column(col, justify=just, no_wrap=True)
    for row in r.by_position:
        weak = row.weakest
        best = row.best
        b.add_row(
            row.pos,
            weak.row.name if weak else "[red](empty slot)[/]",
            f"{weak.ros:.0f}" if weak else "–",
            best.pickup.row.name if best else "[dim]nobody better[/]",
            f"{best.pickup.ros:.0f}" if best else "",
            f"{best.gain:+.1f}" if best else "",
        )
    out.append(b)

    s = Table(
        title=f"Streamers for period {r.period.number} (gain = better lineup this week)", title_justify="left"
    )
    for col, just in [
        ("Stream", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("", "left"),
        ("Games", "right"),
        ("Exp FP wk", "right"),
        ("Drop", "left"),
        ("Week gain", "right"),
        ("ROS change", "right"),
    ]:
        s.add_column(col, justify=just, no_wrap=True)
    for o in r.streamers:
        p = o.pickup
        s.add_row(
            p.row.name,
            p.row.positions,
            p.row.nhl_team or "",
            _status(p.row.owner),
            _games_cell(p),
            f"{p.week:.1f}",
            o.drop.row.name if o.drop else "[green](open spot)[/]",
            f"[bold]{o.gain:+.1f}[/]",
            f"{o.ros_change:+.0f}",
        )
    if not r.streamers:
        s.add_row("[dim]No streamer beats your lineup this week[/]", *[""] * 8)
    out.append(s)

    if r.density:
        out.append(
            "[bold]Games per team this period:[/] "
            + " · ".join(f"{n}: {', '.join(teams)}" for n, teams in r.density.items())
        )
    out.append("[dim]Recommendations only: make any claims or drops yourself on Fantrax.[/]")
    if r.notes:
        out.append("\n".join(f"[cyan]•[/] {n}" for n in r.notes))
    if r.uses_moneypuck:
        out.append(f"[dim]{CREDIT}[/]")
    return out


def trade_tables(r) -> list:
    """Renderables for a TradeReport (hockey/trade/analyze.py)."""
    me, them = r.me, r.them

    def side_desc(players, picks):
        items = [p.row.name for p in players] + [str(p) for p in picks]
        return ", ".join(items) or "nothing"

    out: list = [
        f"[bold]Trade:[/] {me.team} sends {side_desc(me.sends, me.picks_sent)} → "
        f"{them.team} sends {side_desc(me.receives, me.picks_received)}"
    ]

    t = Table(title="Players in the deal", title_justify="left")
    for col, just in [
        ("Player", "left"),
        ("Pos", "left"),
        ("NHL", "left"),
        ("Goes to", "left"),
        ("FP/GP", "right"),
        ("ROS FP", "right"),
        ("Over repl.", "right"),
        ("Next season", "right"),
        ("Keeper value", "right"),
        ("Notes", "left"),
    ]:
        t.add_column(col, justify=just, no_wrap=col != "Notes")
    for p, dest in [(p, them.team) for p in me.sends] + [(p, me.team) for p in me.receives]:
        proj = p.row.projection
        t.add_row(
            p.row.name,
            p.row.positions,
            p.row.nhl_team or "",
            dest,
            f"{proj.fp_per_gp:.2f}" if proj else "–",
            f"{p.ros:.0f}",
            f"{p.vor:+.0f}",
            f"{p.next_season:.0f}",
            f"{p.keeper_value:.0f}",
            "; ".join(p.notes),
        )
    out.append(t)

    if r.pick_values:
        pk = Table(title="Draft picks (value = next-season points above waiver level)", title_justify="left")
        for col in ("Pick", "Goes to", "Value"):
            pk.add_column(col)
        for p in me.picks_sent:
            pk.add_row(str(p), them.team, f"{r.pick_values[p]:.0f}")
        for p in me.picks_received:
            pk.add_row(str(p), me.team, f"{r.pick_values[p]:.0f}")
        out.append(pk)

    imp = Table(title="Impact", title_justify="left")
    imp.add_column("")
    imp.add_column(me.team, justify="right")
    imp.add_column(them.team, justify="right")

    def lineup_cell(s):
        return f"{s.lineup_before:.0f} → {s.lineup_after:.0f} ([bold]{s.lineup_change:+.0f}[/])"

    imp.add_row("Starting lineup, rest of season (FP)", lineup_cell(me), lineup_cell(them))

    def signed(x: float) -> str:
        return f"{round(x) or 0:+d}"  # no "-0"

    imp.add_row("Keeper value, next season", signed(me.keeper_change), signed(them.keeper_change))
    if r.pick_values:
        imp.add_row("Draft pick value", signed(me.pick_change), signed(them.pick_change))
    if me.roster_overflow or them.roster_overflow:
        imp.add_row("Must drop to stay legal", str(me.roster_overflow), str(them.roster_overflow))
    imp.add_section()
    imp.add_row(
        f"[bold]Score[/] (lineup + {r.keeper_weight:g} × future)",
        f"[bold]{me.score:+.0f}[/]",
        f"[bold]{them.score:+.0f}[/]",
    )
    out.append(imp)

    color = "green" if me.score > 0 else "red"
    out.append(f"[bold {color}]{r.verdict}[/]")
    repl = " · ".join(f"{pos} {v:.0f}" for pos, v in r.replacement.items())
    out.append(
        f"[dim]Replacement level (ROS FP at the first non-starter): {repl} · keeper line: "
        f"{r.keeper_line:.0f} next-season FP[/]"
    )
    out.append("[dim]Analysis only: this tool never sends or accepts trade offers.[/]")
    if r.notes:
        out.append("\n".join(f"[cyan]•[/] {n}" for n in r.notes))
    if r.uses_moneypuck:
        out.append(f"[dim]{CREDIT}[/]")
    return out


def keeper_tables(team_name: str, p, horizon: int) -> list:
    """Renderables for a KeeperPlan (hockey/keeper/plan.py)."""
    labels = {
        "regular": "[green]Keep[/]",
        "regular+tag": "[green]Keep + tag[/]",
        "minors": "[cyan]Keep (minors)[/]",
        "release": "[dim]Release[/]",
    }
    order = {"regular+tag": 0, "regular": 1, "minors": 2, "release": 3}
    t = Table(
        title=f"{team_name}: keeper plan ({horizon}-season value, aged and discounted)", title_justify="left"
    )
    for col, just in [
        ("Decision", "left"),
        ("Player", "left"),
        ("Pos", "left"),
        ("Age", "right"),
        ("Career GP", "right"),
        ("Clock", "left"),
        ("Next season", "right"),
        (f"In {horizon} yrs", "right"),
        ("Keeper value", "right"),
        ("Notes", "left"),
    ]:
        t.add_column(col, justify=just, no_wrap=col != "Notes")
    for c in sorted(p.candidates, key=lambda c: (order[c.choice], -c.value)):
        e = c.entry
        if c.minors_eligible:
            clock = "minors (clock off)"
        elif e.franchise_tag:
            clock = f"tagged, kept {e.times_kept}x"
        else:
            clock = f"kept {e.times_kept}x" + ("" if c.in_yaml else " (assumed)")
        gp = f"{c.career_gp}" + (f" → {c.career_gp_end:.0f}" if round(c.career_gp_end) != c.career_gp else "")
        t.add_row(
            labels[c.choice],
            c.row.name,
            c.row.positions,
            f"{c.age:.0f}" if c.age is not None else "?",
            gp,
            clock,
            f"{c.yearly[0]:.0f}" if c.yearly else "–",
            f"{c.yearly[-1]:.0f}" if c.yearly else "–",
            f"{c.value:.0f}" if c.choice != "release" else "",
            "; ".join(c.notes),
        )
    out: list = [t]
    tags = [c.row.name for c in p.candidates if c.choice == "regular+tag"]
    out.append(
        f"[bold]Regular keepers:[/] {len(p.chosen('regular'))}/{p.rules.regular} · "
        f"[bold]franchise tags:[/] {', '.join(tags) or 'none needed'} ({len(tags)}/{p.rules.tags}) · "
        f"[bold]minors:[/] {len(p.chosen('minors'))}/{p.rules.minors} · total value {p.total:.0f}"
    )
    out.append(
        f"[dim]Keeper value = projected points above the keeper line ({p.keeper_line:.0f} next-season FP, "
        f"about what the draft replaces a keeper with), for each season he can still be kept; minors use raw "
        "points. Ages as of next Oct 1; age curve in hockey/keeper/aging.py.[/]"
    )
    if p.warnings:
        out.append("\n".join(f"[yellow]![/] {w}" for w in p.warnings))
    out.append(
        "[dim]Clock data comes from data/keepers.yaml: keep it current (times kept, tags, removed tags).[/]"
    )
    if p.uses_moneypuck:
        out.append(f"[dim]{CREDIT}[/]")
    return out


def _empty_text(empty: dict[str, int]) -> str:
    return ("empty: " + ", ".join(f"{n} {pos}" for pos, n in empty.items())) if empty else ""


def _rank_cell(rank: int, n: int) -> str:
    cut = max(1, round(n * 0.3))
    if rank <= cut:
        return f"[green]{rank}[/]"
    if rank > n - cut:
        return f"[red]{rank}[/]"
    return str(rank)


def intel_tables(league, period=None) -> list:
    """Renderables for LeagueIntel (hockey/intel/league.py)."""
    n = len(league.teams)
    slots = list(league.me.by_slot)
    t = Table(
        title="League: starting-lineup strength (rest of season) and rank at each slot (1 = best)",
        title_justify="left",
    )
    for col, just in (
        [("#", "right"), ("Team", "left"), ("Lineup FP", "right")]
        + [(s, "right") for s in slots]
        + [("Strengths", "left"), ("Weaknesses", "left")]
    ):
        t.add_column(col, justify=just, no_wrap=True)
    for p in league.teams:
        name = f"[bold]{p.name}[/]" if p.team_id == league.me.team_id else p.name
        t.add_row(
            str(p.overall_rank),
            name,
            f"{p.lineup:.0f}",
            *[_rank_cell(p.ranks[s], n) for s in slots],
            ", ".join(p.strengths),
            "; ".join(x for x in [", ".join(p.weaknesses), _empty_text(p.empty)] if x),
        )
    out: list = [t]

    me = league.me
    need = me.weaknesses or [min(me.ranks, key=lambda s: -me.ranks[s])]
    out.append(
        f"[bold]You[/] rank {me.overall_rank}/{n}. Strongest: {', '.join(me.strengths) or 'none'} · "
        f"needs: {', '.join(need)}" + (f" · {_empty_text(me.empty)}" if me.empty else "")
    )

    if league.opponent:
        o = league.opponent
        when = f" in period {period.number}" if period else ""
        out.append(
            f"[bold]Opponent{when}:[/] {o.name} (rank {o.overall_rank}/{n}, lineup {o.lineup:.0f} ROS FP "
            f"vs your {me.lineup:.0f}) · strong at {', '.join(o.strengths) or 'nothing in particular'} · "
            f"weak at {', '.join(o.weaknesses) or 'nothing in particular'}"
        )

    s = Table(title="Trade partners: 1-for-1 swaps that improve BOTH starting lineups", title_justify="left")
    for col, just in [
        ("Team", "left"),
        ("You give", "left"),
        ("You get", "left"),
        ("Your gain", "right"),
        ("Their gain", "right"),
        ("Their needs", "left"),
    ]:
        s.add_column(col, justify=just, no_wrap=True)
    for team, swaps in league.partners:
        for i, sw in enumerate(swaps):
            s.add_row(
                team.name if i == 0 else "",
                f"{sw.give.name} ({','.join(sw.give.eligible)})",
                f"{sw.get.name} ({','.join(sw.get.eligible)})",
                f"{sw.my_gain:+.0f}",
                f"{sw.their_gain:+.0f}",
                ", ".join(team.weaknesses) if i == 0 else "",
            )
    if not league.partners:
        s.add_row("[dim]No 1-for-1 swap helps both lineups right now[/]", "", "", "", "", "")
    out.append(s)
    out.append(
        "[dim]Gains are rest-of-season starting-lineup points. Check any idea with `hockey trade GIVE GET` "
        "(adds keeper value and roster space).[/]"
    )
    if league.uses_moneypuck:
        out.append(f"[dim]{CREDIT}[/]")
    return out


NEWS_STYLE = {
    "out": "bold red",
    "day_to_day": "yellow",
    "role_up": "green",
    "role_down": "yellow",
    "starting_goalie": "cyan",
    "other": "",
}


def news_table(news, only: set[str] | None = None, title: str | None = None) -> Table | None:
    """AI news flags (hockey/ai/news.py). ``only``: limit to these player names (accent/case-insensitive)."""
    from hockey.idmap.normalize import basic

    keep = {basic(n) for n in only} if only is not None else None
    flags = [f for f in news.flags if keep is None or basic(f["player"]) in keep]
    if not flags:
        return None
    age = news.age_hours()
    t = Table(
        title=title or f"AI news flags (checked {age:.0f} h ago; judgment from news, not the engine)",
        title_justify="left",
    )
    for c in ("Player", "Status", "Note", "Source", "As of"):
        t.add_column(c, overflow="fold")
    order = list(NEWS_STYLE)
    for f in sorted(
        flags, key=lambda f: (order.index(f["status"]) if f["status"] in order else 99, f["player"])
    ):
        st = NEWS_STYLE.get(f["status"], "")
        t.add_row(
            f["player"],
            f"[{st}]{f['status']}[/]" if st else f["status"],
            f["note"],
            f.get("source_url") or "",
            f.get("as_of") or "",
        )
    return t


def kv_table(title: str, data: dict) -> Table:
    t = Table(title=title, title_justify="left", show_header=False)
    t.add_column(style="bold")
    t.add_column(justify="right")
    for k, v in data.items():
        t.add_row(str(k), str(v))
    return t


console = Console()
