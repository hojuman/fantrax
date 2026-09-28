"""Fallback: CSVs exported by hand from Fantrax (Players page or a team roster, "Download CSV").

Column names drift over time, so lookups go through small alias lists and the importer fails with
the header it actually saw.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from hockey.sources.fantrax_fxea import display_name
from hockey.sources.teams import normalize_team, pos_group

ID_COLS = ("ID", "Id", "Player ID", "PlayerId")
NAME_COLS = ("Player", "Name")
TEAM_COLS = ("Team",)
POS_COLS = ("Position", "Pos", "Eligible")
STATUS_COLS = ("Status", "Owner", "Fantasy Team")
FPTS_COLS = ("FPts", "Fantasy Points", "FP", "Fpts")
NON_STAT_COLS = {"RkOv", "Rk", "Age", "Opponent", "Salary", "Contract", "FP/G", "% Owned", "+/- Own", "ADP"}


class CsvFormatError(Exception):
    pass


@dataclass
class CsvPlayer:
    fantrax_id: str
    name: str
    nhl_team: str | None
    positions: str
    pos_group: str | None
    status: str | None  # fantasy team short name, "FA", "W (Tue)", ...
    fpts: float | None
    stats: dict[str, float] = field(default_factory=dict)  # remaining numeric columns, raw header -> value


def _pick(header: list[str], options: tuple[str, ...]) -> str | None:
    lower = {h.strip().lower(): h for h in header}
    return next((lower[o.lower()] for o in options if o.lower() in lower), None)


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace(",", "")
    if v in ("", "-", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def read_players_csv(path: Path | str) -> list[CsvPlayer]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        id_col, name_col = _pick(header, ID_COLS), _pick(header, NAME_COLS)
        if not id_col or not name_col:
            raise CsvFormatError(f"Need an ID and a Player column; header was: {header}")
        team_col, pos_col = _pick(header, TEAM_COLS), _pick(header, POS_COLS)
        status_col, fpts_col = _pick(header, STATUS_COLS), _pick(header, FPTS_COLS)
        used = {id_col, name_col, team_col, pos_col, status_col, fpts_col}
        out = []
        for row in reader:
            fid = (row.get(id_col) or "").strip().strip("*")
            if not fid:
                continue
            positions = (row.get(pos_col) or "") if pos_col else ""
            stats = {
                h: n
                for h in header
                if h not in used and h not in NON_STAT_COLS and (n := _num(row.get(h))) is not None
            }
            out.append(
                CsvPlayer(
                    fantrax_id=fid,
                    name=display_name(row[name_col]),
                    nhl_team=normalize_team(row.get(team_col)) if team_col else None,
                    positions=positions,
                    pos_group=pos_group(positions),
                    status=((row.get(status_col) or "").strip() or None) if status_col else None,
                    fpts=_num(row.get(fpts_col)) if fpts_col else None,
                    stats=stats,
                )
            )
    return out


def is_rostered_status(status: str | None) -> bool:
    if not status:
        return False
    s = status.strip().upper()
    return not (s == "FA" or s.startswith("W ") or s == "W" or s.startswith("W("))
