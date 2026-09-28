"""NHL team codes, and Fantrax's variants of them."""

from __future__ import annotations

NHL_TEAMS = (
    "ANA",
    "BOS",
    "BUF",
    "CGY",
    "CAR",
    "CHI",
    "COL",
    "CBJ",
    "DAL",
    "DET",
    "EDM",
    "FLA",
    "LAK",
    "MIN",
    "MTL",
    "NSH",
    "NJD",
    "NYI",
    "NYR",
    "OTT",
    "PHI",
    "PIT",
    "SJS",
    "SEA",
    "STL",
    "TBL",
    "TOR",
    "UTA",
    "VAN",
    "VGK",
    "WSH",
    "WPG",
)

# Non-NHL spellings seen on fantasy sites -> NHL abbreviation.
TEAM_ALIASES = {
    "TB": "TBL",
    "NJ": "NJD",
    "LA": "LAK",
    "SJ": "SJS",
    "CLS": "CBJ",
    "CLB": "CBJ",
    "MON": "MTL",
    "WAS": "WSH",
    "VGS": "VGK",
    "VEG": "VGK",
    "NAS": "NSH",
    "CAL": "CGY",
    "WIN": "WPG",
    "ARI": "UTA",
    "UTAH": "UTA",
    "UHC": "UTA",
    "PHO": "UTA",
}
NO_TEAM = {"", "FA", "(N/A)", "N/A", "NA", "-", "--", "UFA", "RFA"}


def normalize_team(code: str | None) -> str | None:
    """Return the NHL abbreviation for a team code, or None for free agents / unknown."""
    if code is None:
        return None
    c = code.strip().upper()
    if c in NO_TEAM:
        return None
    c = TEAM_ALIASES.get(c, c)
    return c if c in NHL_TEAMS else None


def pos_group(positions: str | None) -> str | None:
    """Fantrax position string ("C,LW", "D", "G", "F") or NHL positionCode ("L", "R") -> F / D / G."""
    if not positions:
        return None
    parts = {p.strip().upper() for p in positions.replace("/", ",").split(",") if p.strip()}
    if "G" in parts:
        return "G"
    if parts & {"C", "LW", "RW", "L", "R", "F", "W"}:
        return "F"
    if "D" in parts:
        return "D"
    return None
