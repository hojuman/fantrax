"""The conversation loop: Claude calls the read-only tools (and web search), then answers.

A manual loop rather than the SDK's beta tool runner, so `pause_turn` (a long server-side web search)
resumes cleanly and the whole thing can be tested with a scripted fake client.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from hockey.ai.client import AiClient, AiError


@dataclass
class Answer:
    text: str
    stop_reason: str
    tool_calls: list[str] = field(default_factory=list)
    searches: int = 0
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url) cited by web search
    uses_moneypuck: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"


class NoTools:
    """Executor for conversations that use only web search (news) or nothing at all (the report take)."""

    uses_moneypuck = False

    def definitions(self) -> list[dict]:
        return []

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        return f"No tool named {name!r} here", True


def _add_usage(ans: Answer, msg: Any) -> None:
    u = getattr(msg, "usage", None)
    if u is None:
        return
    ans.input_tokens += getattr(u, "input_tokens", 0) or 0
    ans.output_tokens += getattr(u, "output_tokens", 0) or 0
    ans.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0


def converse(
    client: AiClient,
    system: list[dict],
    messages: list[dict],
    executor: Any = None,
    *,
    web: bool = False,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[str, dict], None] | None = None,
) -> Answer:
    """Run until Claude stops calling tools. ``messages`` is extended in place (for follow-up questions)."""
    executor = executor or NoTools()
    tools = executor.definitions() + ([client.settings.web_search_tool()] if web else [])
    ans = Answer("", "")
    for _ in range(client.settings.max_turns):
        params: dict[str, Any] = {"system": system, "messages": messages}
        if tools:
            params["tools"] = tools
        msg = client.create(on_text=on_text, **params)
        _add_usage(ans, msg)
        ans.stop_reason = msg.stop_reason
        if msg.stop_reason == "refusal":
            raise AiError("Claude declined to answer this one.")
        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for block in msg.content:
            kind = getattr(block, "type", None)
            if kind == "server_tool_use" and getattr(block, "name", "") == "web_search":
                ans.searches += 1
                if on_tool:
                    on_tool("web_search", getattr(block, "input", {}) or {})
            elif kind == "tool_use":
                if on_tool:
                    on_tool(block.name, block.input or {})
                content, is_error = executor.run(block.name, block.input or {})
                ans.tool_calls.append(block.name)
                result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            elif kind == "text":
                for c in getattr(block, "citations", None) or []:
                    url = getattr(c, "url", None)
                    if url and url not in {u for _, u in ans.sources}:
                        ans.sources.append((getattr(c, "title", None) or url, url))
        if msg.stop_reason == "pause_turn":
            continue  # a long server-side search paused; resending the history resumes it
        if msg.stop_reason == "tool_use" and results:
            messages.append({"role": "user", "content": results})
            continue
        ans.text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
        break
    else:
        raise AiError(f"Gave up after {client.settings.max_turns} tool rounds without a final answer.")
    ans.uses_moneypuck = bool(getattr(executor, "uses_moneypuck", False))
    return ans
