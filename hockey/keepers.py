"""The hand-maintained keeper clock in data/keepers.yaml (Fantrax doesn't track the league's rules).

Phase 4 uses it to protect franchise-tagged players from drop suggestions and to warn before
suggesting a drop of someone with keeper history. Phase 6 will use the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from hockey.config import DATA_DIR


@dataclass(frozen=True)
class KeeperEntry:
    fantrax_id: str
    player: str
    times_kept: int = 0
    franchise_tag: bool = False
    tag_removed: bool = False


def load_keepers(path: Path | None = None) -> dict[str, KeeperEntry]:
    path = path or DATA_DIR / "keepers.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out = {}
    for p in data.get("players") or []:
        fid = str(p.get("fantrax_id") or "").strip()
        if fid:
            out[fid] = KeeperEntry(
                fantrax_id=fid,
                player=str(p.get("player") or fid),
                times_kept=int(p.get("times_kept") or 0),
                franchise_tag=bool(p.get("franchise_tag")),
                tag_removed=bool(p.get("tag_removed")),
            )
    return out
