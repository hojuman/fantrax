"""News flags: Claude searches recent news for your roster and the best free agents, then the findings
are turned into structured flags that the lineup and waiver views can use.

Flags are advisory. An `out` flag is treated like `--out` by `hockey lineup --ai` (you can override it
with `--play NAME`); everything else is shown next to the player, never folded silently into the numbers.
Stored in the `meta` table (key `ai_news`) and reused for `ai.news_ttl_hours`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from hockey.ai.agent import converse
from hockey.ai.client import AiClient, AiUnavailable
from hockey.ai.prompts import NEWS_EXTRACT, NEWS_TASK, system_blocks
from hockey.context import LeagueContext
from hockey.db import get_meta, set_meta
from hockey.idmap.normalize import basic
from hockey.valuation import RosterRow

META_KEY = "ai_news"
FREE_AGENTS = 15  # the top free agents by ROS FP are checked too (pickup candidates)

Status = Literal["out", "day_to_day", "role_up", "role_down", "starting_goalie", "other"]


class NewsFlag(BaseModel):
    player: str
    status: Status
    note: str
    source_url: str | None = None
    as_of: str | None = None  # the source's date, as written


class NewsFlags(BaseModel):
    flags: list[NewsFlag]


@dataclass
class News:
    checked_at: datetime
    flags: list[dict]  # NewsFlag fields + fantrax_id (None if the name didn't match a checked player)
    sources: list[tuple[str, str]] = field(default_factory=list)
    searches: int = 0

    def age_hours(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.checked_at).total_seconds() / 3600

    def by_status(self, status: str) -> list[dict]:
        return [f for f in self.flags if f["status"] == status]

    def for_name(self, name: str) -> dict | None:
        key = basic(name)
        return next((f for f in self.flags if basic(f["player"]) == key), None)


def watchlist(ctx: LeagueContext, free_agents: int = FREE_AGENTS) -> list[RosterRow]:
    mine = ctx.valuer.team_roster(ctx.me["team_id"])
    fas = sorted(
        (r for r in ctx.valuer.league_players() if r.owner in ("FA", "W") and r.projection and r.nhl_id),
        key=lambda r: -r.projection.ros_fp,
    )[:free_agents]
    return mine + fas


def _line(r: RosterRow, mine: bool) -> str:
    who = f"your {(r.status or 'roster').lower()}" if mine else "free agent"
    return f"- {r.name} ({r.positions}, {r.nhl_team or 'no NHL team'}; {who})"


def scan(
    ctx: LeagueContext,
    client: AiClient,
    *,
    on_tool: Callable[[str, dict], None] | None = None,
    now: datetime | None = None,
) -> News:
    """Search the news for the watchlist, extract flags, store them. Needs web search."""
    if not client.settings.web_search:
        raise AiUnavailable("News needs web search: set ai.web_search: true in data/league.yaml")
    rows = watchlist(ctx)
    mine_ids = {r.fantrax_id for r in ctx.valuer.team_roster(ctx.me["team_id"])}
    players = "\n".join(_line(r, r.fantrax_id in mine_ids) for r in rows)
    system = system_blocks(ctx.league, ctx.valuer.rules)
    today = (now or datetime.now(UTC)).date().isoformat()
    research = converse(
        client,
        system,
        [{"role": "user", "content": f"{NEWS_TASK}\n\nToday is {today}.\n\nPlayers:\n{players}"}],
        web=True,
        on_tool=on_tool,
    )
    parsed = client.parse(
        NewsFlags,
        messages=[
            {
                "role": "user",
                "content": f"{NEWS_EXTRACT}\n\nPlayers:\n{players}\n\nNotes:\n{research.text or '(no news found)'}",
            }
        ],
    )
    ids = {basic(r.name): r.fantrax_id for r in rows}
    flags = [f.model_dump() | {"fantrax_id": ids.get(basic(f.player))} for f in parsed.flags]
    news = News(now or datetime.now(UTC), flags, research.sources, research.searches)
    save(ctx.conn, news)
    return news


def save(conn: sqlite3.Connection, news: News) -> None:
    set_meta(
        conn,
        META_KEY,
        {
            "checked_at": news.checked_at.isoformat(),
            "flags": news.flags,
            "sources": news.sources,
            "searches": news.searches,
        },
    )
    conn.commit()


def load(conn: sqlite3.Connection) -> News | None:
    data = get_meta(conn, META_KEY)
    if not data:
        return None
    return News(
        datetime.fromisoformat(data["checked_at"]),
        data.get("flags") or [],
        [tuple(s) for s in data.get("sources") or []],
        data.get("searches", 0),
    )


def fresh(conn: sqlite3.Connection, ttl_hours: float, now: datetime | None = None) -> News | None:
    news = load(conn)
    if news and news.checked_at > (now or datetime.now(UTC)) - timedelta(hours=ttl_hours):
        return news
    return None


def outs(news: News | None, play: list[str] | None = None) -> list[dict]:
    """`out` flags, minus players the user says will play (`--play`)."""
    keep = {basic(n) for n in play or []}
    return [f for f in (news.by_status("out") if news else []) if basic(f["player"]) not in keep]
