"""The Anthropic client, wrapped so the rest of the AI layer (and the tests) see two small calls.

Settings come from the `ai:` block of data/league.yaml; the key from ANTHROPIC_API_KEY (in .env, which
is gitignored). The SDK talks to api.anthropic.com directly, not through hockey/http.py: that is the
only non-GET request this tool makes, and it never goes to Fantrax.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_DOMAINS = ("nhl.com", "dailyfaceoff.com", "sportsnet.ca", "tsn.ca", "espn.com")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"
NEVER_SEARCH = ("fantrax.com",)  # Fantrax's ToS prohibits crawling; keep it out of web search too


class AiUnavailable(Exception):
    """The AI layer is switched off (no package / no key). The message says how to turn it on."""


class AiError(Exception):
    """The model declined, or the conversation couldn't finish."""


@dataclass
class AiSettings:
    model: str = DEFAULT_MODEL
    effort: str = "medium"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    fallbacks: bool = True
    web_search: bool = True
    news_domains: tuple[str, ...] = DEFAULT_DOMAINS
    max_searches: int = 8
    news_ttl_hours: float = 6.0
    max_turns: int = 12  # tool-use round trips per question

    @classmethod
    def from_league(cls, league: dict, **overrides) -> AiSettings:
        cfg = dict(league.get("ai") or {})
        cfg.update({k: v for k, v in overrides.items() if v is not None})
        known = {f for f in cls.__dataclass_fields__}
        s = cls(**{k: v for k, v in cfg.items() if k in known})
        s.news_domains = tuple(
            d for d in (s.news_domains or ()) if not any(d == n or d.endswith("." + n) for n in NEVER_SEARCH)
        )
        return s

    def web_search_tool(self) -> dict:
        tool: dict[str, Any] = {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": self.max_searches}
        if self.news_domains:
            tool["allowed_domains"] = list(self.news_domains)
        else:
            tool["blocked_domains"] = list(NEVER_SEARCH)
        return tool


@dataclass
class AiClient:
    """Streams one request and returns the final message; parses structured output."""

    sdk: Any  # anthropic.Anthropic
    settings: AiSettings = field(default_factory=AiSettings)

    def create(self, *, on_text: Callable[[str], None] | None = None, **params) -> Any:
        s = self.settings
        req = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": s.effort},
            **params,
        }
        if s.fallbacks:
            req["betas"] = [FALLBACK_BETA]
            req["fallbacks"] = "default"
            api = self.sdk.beta.messages
        else:
            api = self.sdk.messages
        with api.stream(**req) as stream:
            if on_text:
                for text in stream.text_stream:
                    on_text(text)
            return stream.get_final_message()

    def parse(self, output_format: type, **params) -> Any:
        s = self.settings
        msg = self.sdk.messages.parse(
            model=s.model,
            max_tokens=s.max_tokens,
            output_config={"effort": "low"},
            output_format=output_format,
            **params,
        )
        if msg.stop_reason == "refusal" or msg.parsed_output is None:
            raise AiError("Claude couldn't turn the news into structured flags")
        return msg.parsed_output


def make_client(league: dict, **overrides) -> AiClient:
    """An AiClient, or AiUnavailable with a one-line fix."""
    try:
        import anthropic
    except ImportError as e:
        raise AiUnavailable("AI layer off: install it with `uv sync --extra ai`") from e
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise AiUnavailable("AI layer off: set ANTHROPIC_API_KEY in .env (see .env.example)")
    return AiClient(anthropic.Anthropic(), AiSettings.from_league(league, **overrides))
