"""The daily markdown report: this week's lineup, pickups and streamers, and league intel on one page.

Pure rendering: it takes the report objects the individual commands already build (lineup, waivers,
intel) and writes markdown. Nothing here fetches data or talks to Fantrax.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from hockey.sources.moneypuck import CREDIT


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
    return out + [""]


def _lineup(r, now: datetime) -> list[str]:
    hours = (r.period.start - now).total_seconds() / 3600
    lock = f"lock {r.period.start.astimezone():%a %b %d %H:%M}" + (
        f", in {hours:.0f} h" if hours > 0 else ", locked"
    )
    lines = [f"## Lineup: period {r.period.number} ({lock}, {r.games_in_period} NHL games)", ""]
    if r.moves_in or r.moves_out:
        lines += [f"- **Move {w.row.name} to Active** ({w.slot})" for w in r.moves_in]
        lines += [f"- **Move {w.row.name} to Reserve**" for w in r.moves_out]
    else:
        lines.append("- Your current lineup is already optimal.")
    lines += [
        f"- Projected: current {r.current_total:.1f} FP → recommended {r.total:.1f} FP ({r.gain:+.1f})",
        "",
    ]
    rows = [
        [
            w.slot,
            w.row.name,
            w.row.nhl_team or "",
            str(w.avail.games) + (" b2b" if w.avail.b2b else ""),
            f"{w.value:.1f}",
        ]
        for w in r.starters
    ]
    rows += [[pos, "(empty)", "", "", ""] for pos, n in r.empty.items() for _ in range(n)]
    lines += _table(["Slot", "Player", "NHL", "Games", "Exp FP"], rows)
    if r.flags:
        lines += [f"- ⚠ {f}" for f in r.flags] + [""]
    return lines


def _waivers(w, limit: int = 5) -> list[str]:
    lines = [f"## Waivers ({w.open_spots} open roster spot{'' if w.open_spots == 1 else 's'})", ""]
    if w.pickups:
        lines += _table(
            ["Pick up", "Pos", "NHL", "ROS FP", "Drop", "ROS gain"],
            [
                [
                    o.pickup.row.name + (" (W)" if o.pickup.row.owner == "W" else ""),
                    o.pickup.row.positions,
                    o.pickup.row.nhl_team or "",
                    f"{o.pickup.ros:.0f}",
                    o.drop.row.name if o.drop else "(open spot)",
                    f"{o.gain:+.1f}",
                ]
                for o in w.pickups[:limit]
            ],
        )
    else:
        lines += ["- No free agent improves your starting lineup.", ""]
    if w.depth:
        lines += [
            "Best depth for your open spot: "
            + ", ".join(f"{p.row.name} ({p.row.positions}, {p.ros:.0f} ROS FP)" for p in w.depth[:3]),
            "",
        ]
    if w.streamers:
        lines += [f"**Streamers for period {w.period.number}:**", ""]
        lines += _table(
            ["Stream", "Pos", "NHL", "Games", "Drop", "Week gain", "ROS change"],
            [
                [
                    o.pickup.row.name,
                    o.pickup.row.positions,
                    o.pickup.row.nhl_team or "",
                    str(o.pickup.avail.games),
                    o.drop.row.name if o.drop else "(open spot)",
                    f"{o.gain:+.1f}",
                    f"{o.ros_change:+.0f}",
                ]
                for o in w.streamers[:limit]
            ],
        )
    lines += [f"- {n}" for n in w.notes if "no NHL record" not in n] + [""]
    return lines


def _intel(league, limit: int = 3) -> list[str]:
    me, n = league.me, len(league.teams)
    lines = [
        "## League",
        "",
        f"- You rank **{me.overall_rank}/{n}** by starting lineup ({me.lineup:.0f} ROS FP). "
        f"Strong: {', '.join(me.strengths) or 'nothing stands out'}; weak: {', '.join(me.weaknesses) or 'none'}.",
    ]
    if league.opponent:
        o = league.opponent
        lines.append(
            f"- This week's opponent: **{o.name}** (rank {o.overall_rank}/{n}, {o.lineup:.0f} ROS FP); "
            f"strong at {', '.join(o.strengths) or '–'}, weak at {', '.join(o.weaknesses) or '–'}."
        )
    swaps = sorted((s for _, ss in league.partners for s in ss), key=lambda s: -s.mutual)[:limit]
    if swaps:
        lines += ["- Trade ideas that help both lineups:"]
        lines += [
            f"  - {s.give.name} → {s.team} for {s.get.name} (you {s.my_gain:+.0f}, them {s.their_gain:+.0f}); "
            f'check with `hockey trade "{s.give.name}" "{s.get.name}"`'
            for s in swaps
        ]
    return lines + [""]


def render(
    team_name: str, now: datetime, *, lineup=None, waivers=None, league=None, errors: list[str] | None = None
) -> str:
    lines = [
        f"# {team_name}: daily report, {now.astimezone():%A %B %d, %Y}",
        "",
        f"_Generated {now.astimezone():%H:%M}. Recommendations only: make any moves yourself on Fantrax._",
        "",
    ]
    for e in errors or []:
        lines.append(f"> ⚠ {e}")
    if errors:
        lines.append("")
    if lineup:
        lines += _lineup(lineup, now)
    if waivers:
        lines += _waivers(waivers)
    if league:
        lines += _intel(league)
    if any(x and x.uses_moneypuck for x in (lineup, waivers, league)):
        lines += ["---", f"_{CREDIT}_", ""]
    return "\n".join(lines)


def write(markdown: str, out_dir: Path, now: datetime) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now.astimezone():%Y-%m-%d}.md"
    path.write_text(markdown)
    return path
