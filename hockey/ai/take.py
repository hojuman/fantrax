"""The "AI take" section of the daily report: Claude reads the engine's report and the news flags."""

from __future__ import annotations

from hockey.ai.agent import Answer, converse
from hockey.ai.client import AiClient
from hockey.ai.news import News
from hockey.ai.prompts import REPORT_TASK, system_blocks
from hockey.context import LeagueContext


def news_lines(news: News | None) -> str:
    if not news or not news.flags:
        return "(no news flags)"
    return "\n".join(
        f"- {f['player']}: {f['status']}: {f['note']}"
        + (f" ({f['source_url']})" if f.get("source_url") else "")
        for f in news.flags
    )


def report_take(ctx: LeagueContext, client: AiClient, report_markdown: str, news: News | None) -> Answer:
    content = f"{REPORT_TASK}\n\n# Engine report\n\n{report_markdown}\n\n# News flags\n\n{news_lines(news)}"
    return converse(
        client, system_blocks(ctx.league, ctx.valuer.rules), [{"role": "user", "content": content}]
    )
