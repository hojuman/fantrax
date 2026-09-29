"""The AI layer, offline: a scripted fake Anthropic SDK stands in for the API."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from hockey import cli
from hockey.ai import news as ai_news
from hockey.ai.agent import converse
from hockey.ai.client import AiClient, AiError, AiSettings, AiUnavailable, make_client
from hockey.ai.prompts import system_blocks
from hockey.ai.tools import BY_NAME, READ_ONLY_TOOLS, Executor
from hockey.context import LeagueContext, my_team
from hockey.lineup.report import build_report
from hockey.report.daily import render
from hockey.sync import load_rules
from hockey.valuation import Valuer
from tests.conftest import FakeHttp
from tests.test_intel_report import cli_db  # noqa: F401  (fixture)
from tests.test_lineup import P2
from tests.test_sync_csv import MINE, full_sync

# --- fake SDK -------------------------------------------------------------------------------------


def text(t, citations=None):
    return SimpleNamespace(type="text", text=t, citations=citations)


def tool_use(name, args, id="tu1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=args)


def search(query):
    return SimpleNamespace(type="server_tool_use", id="st1", name="web_search", input={"query": query})


def message(stop, *content):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, content=list(content), usage=usage)


class _Stream:
    def __init__(self, msg):
        self.msg = msg
        self.text_stream = [b.text for b in msg.content if b.type == "text"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeSdk:
    """Replays scripted messages; records every request (as a snapshot, since messages grow in place)."""

    def __init__(self, replies, parsed=None):
        self.replies = list(replies)
        self.parsed = parsed
        self.requests: list[dict] = []
        self.parse_requests: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream, parse=self._parse)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **req):
        self.requests.append(req | {"messages": list(req["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _Stream(reply)

    def _parse(self, **req):
        self.parse_requests.append(req)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.parsed)


def fake_client(replies, parsed=None, **settings):
    return AiClient(FakeSdk(replies, parsed), AiSettings(**settings))


# --- fixtures -------------------------------------------------------------------------------------


@pytest.fixture
def ctx(conn, settings, monkeypatch):
    full_sync(conn, settings)
    from hockey.sources.nhl import NhlClient

    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    monkeypatch.setattr(NhlClient, "schedule", lambda self, s, e: games)
    return LeagueContext(settings, conn, FakeHttp(), my_team(conn, settings))


# --- settings and client ----------------------------------------------------------------------------


def test_settings_never_search_fantrax():
    s = AiSettings.from_league({"ai": {"news_domains": ["nhl.com", "fantrax.com", "www.fantrax.com"]}})
    assert s.news_domains == ("nhl.com",)
    assert s.web_search_tool()["allowed_domains"] == ["nhl.com"]
    empty = AiSettings.from_league({"ai": {"news_domains": []}})
    assert empty.web_search_tool()["blocked_domains"] == ["fantrax.com"]
    assert AiSettings.from_league({}, model="claude-sonnet-5-5").model == "claude-sonnet-5-5"


def test_no_key_means_ai_off(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(AiUnavailable, match="ANTHROPIC_API_KEY"):
        make_client({})


def test_request_shape():
    client = fake_client([message("end_turn", text("hi"))])
    seen = []
    client.create(on_text=seen.append, system=[], messages=[{"role": "user", "content": "q"}])
    req = client.sdk.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["thinking"] == {"type": "adaptive"}
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"] == {"effort": "medium"} and seen == ["hi"]
    plain = fake_client([message("end_turn", text("hi"))], fallbacks=False)
    plain.create(system=[], messages=[])
    assert "fallbacks" not in plain.sdk.requests[0] and "betas" not in plain.sdk.requests[0]


def test_system_prompt_is_stable_and_cacheable(ctx):
    a = system_blocks(ctx.league, ctx.valuer.rules)
    b = system_blocks(ctx.league, load_rules(ctx.conn))
    assert a == b and a[-1]["cache_control"] == {"type": "ephemeral"}
    assert "You only advise" in a[0]["text"] and '"sv": 0.155' in a[0]["text"]


# --- tools ----------------------------------------------------------------------------------------


def test_tool_registry_is_read_only():
    assert READ_ONLY_TOOLS == (
        "get_roster",
        "get_player",
        "rank_players",
        "best_lineup",
        "waiver_options",
        "evaluate_trade",
        "keeper_plan",
        "league_intel",
        "schedule",
    )
    for name in READ_ONLY_TOOLS:
        assert not any(w in name for w in ("add", "drop", "claim", "set", "move", "post", "trade_offer"))
        assert BY_NAME[name].schema["additionalProperties"] is False


def run(ex, tool_name, **args):
    out, err = ex.run(tool_name, args)
    assert not err, out
    return json.loads(out)


def test_tools_on_fixture_league(ctx):
    ex = Executor(ctx)
    roster = run(ex, "get_roster")
    assert roster["team"] == "The Blue Blazers" and any(
        p["name"] == "Quinn Hughes" for p in roster["players"]
    )
    assert "ros_fp" in roster["players"][0]
    assert run(ex, "get_roster", team="Team 03")["team"] == "Team 03"

    mcd = run(ex, "get_player", name="McDavid")
    assert mcd["name"] == "Connor McDavid" and mcd["fp_per_gp_by_stat"] and "method" in mcd

    fas = run(ex, "rank_players", pos="D", available_only=True, limit=5)["players"]
    assert 0 < len(fas) <= 5 and all(p["owner"] in ("FA", "W") for p in fas)

    lu = run(ex, "best_lineup", period=2, out=["Quinn Hughes"])
    assert lu["period"] == 2 and lu["starters"]
    assert "Quinn Hughes" not in {p["name"] for p in lu["starters"]}

    w = run(ex, "waiver_options", period=2)
    assert {"open_roster_spots", "pickups_ros_gain", "streamers_week_gain", "by_position"} <= set(w)

    t = run(ex, "evaluate_trade", give="Stutzle", get="Demko")
    assert t["you"]["receives"][0]["name"] == "Thatcher Demko" and t["verdict"]

    li = run(ex, "league_intel")
    assert li["you"]["team"] == "The Blue Blazers" and len(li["teams"]) >= 2

    sched = run(ex, "schedule", period=2)
    assert sched["period"] == 2 and sched["games"] > 0

    kp = run(ex, "keeper_plan")
    assert kp["players"] and {"choice", "value"} <= set(kp["players"][0])

    assert ex.uses_moneypuck  # fixture projections use MoneyPuck, so answers must carry the credit
    assert "_uses_moneypuck" not in json.dumps(lu)


def test_tool_errors_go_back_to_the_model(ctx):
    ex = Executor(ctx)
    assert ex.run("drop_player", {"name": "x"})[1]
    msg, err = ex.run("get_roster", {"team": "Nobody FC"})
    assert err and "No fantasy team" in msg
    msg, err = ex.run("get_player", {"nam": "x"})
    assert err and "Unknown argument" in msg
    msg, err = ex.run("evaluate_trade", {"give": "Pettersson", "get": "Demko"})
    assert err and "ambiguous" in msg
    msg, err = ex.run("best_lineup", {"period": 999})
    assert err and "period" in msg


# --- the loop -------------------------------------------------------------------------------------


def test_converse_runs_tools_and_resumes_paused_turns(ctx):
    cite = SimpleNamespace(url="https://www.nhl.com/news/x", title="Injury update")
    client = fake_client(
        [
            message(
                "tool_use",
                text("Checking."),
                search("Quinn Hughes injury"),
                tool_use("best_lineup", {"period": 2}),
            ),
            message("pause_turn", search("more")),
            message("end_turn", text("Start Demko.", [cite])),
        ]
    )
    ex = Executor(ctx)
    messages = [{"role": "user", "content": "who do I start?"}]
    seen = []
    ans = converse(client, [], messages, ex, web=True, on_tool=lambda n, a: seen.append(n))
    assert ans.text == "Start Demko." and ans.stop_reason == "end_turn"
    assert (
        ans.tool_calls == ["best_lineup"] and ans.searches == 2 and seen[:2] == ["web_search", "best_lineup"]
    )
    assert ans.sources == [("Injury update", "https://www.nhl.com/news/x")] and ans.uses_moneypuck
    reqs = client.sdk.requests
    tools = reqs[0]["tools"]
    assert tools[-1]["type"] == "web_search_20260209" and "fantrax.com" not in json.dumps(tools)
    # 2nd request carries the tool result; the 3rd resends the paused assistant turn as-is.
    result = reqs[1]["messages"][-1]["content"][0]
    assert (
        result["type"] == "tool_result" and result["tool_use_id"] == "tu1" and "starters" in result["content"]
    )
    assert reqs[2]["messages"][-1]["role"] == "assistant"
    assert ans.input_tokens == 300


def api_error(msg, status=400):
    import anthropic
    import httpx2

    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    cls = anthropic.BadRequestError if status == 400 else anthropic.InternalServerError
    return cls(msg, response=response, body=None)


BLOCKED = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
    "\"The following domains are not accessible to our user agent: ['sportsnet.ca', 'tsn.ca']. Read more: ...\"}}"
)


def test_blocked_news_sites_are_dropped_and_retried():
    client = fake_client(
        [api_error(BLOCKED), message("end_turn", text("ok"))],
        news_domains=("nhl.com", "sportsnet.ca", "tsn.ca"),
    )
    ans = converse(client, [], [{"role": "user", "content": "news?"}], web=True)
    assert ans.text == "ok" and client.dropped_domains == ["sportsnet.ca", "tsn.ca"]
    first, retry = client.sdk.requests
    assert "sportsnet.ca" in first["tools"][-1]["allowed_domains"]
    assert retry["tools"][-1]["allowed_domains"] == ["nhl.com"]
    assert client.settings.news_domains == ("nhl.com",)


def test_api_errors_become_ai_errors():
    with pytest.raises(AiError, match="Claude API error"):
        converse(fake_client([api_error("overloaded", 500)]), [], [{"role": "user", "content": "q"}])
    with pytest.raises(ValueError):  # non-API bugs still surface
        converse(fake_client([ValueError("bug")]), [], [{"role": "user", "content": "q"}])


def test_cli_report_ai_survives_api_errors(cli_db, tmp_path, monkeypatch):  # noqa: F811
    client = fake_client([api_error("overloaded", 500), api_error("overloaded", 500)])
    monkeypatch.setattr("hockey.ai.client.make_client", lambda league, **kw: client)
    ok = cli_db.invoke(
        cli.app, ["report", "--ai", "--out-dir", str(tmp_path / "rep")], env={"COLUMNS": "200"}
    )
    assert ok.exit_code == 0, ok.output
    assert "AI news skipped: Claude API error" in ok.output and "AI take skipped" in ok.output
    assert "## Lineup" in next((tmp_path / "rep").glob("*.md")).read_text()


def test_converse_refusal_and_runaway():
    with pytest.raises(AiError, match="declined"):
        converse(fake_client([message("refusal")]), [], [{"role": "user", "content": "q"}])
    loop = [message("tool_use", tool_use("nope", {})) for _ in range(3)]
    with pytest.raises(AiError, match="Gave up"):
        converse(fake_client(loop, max_turns=3), [], [{"role": "user", "content": "q"}])


# --- news -----------------------------------------------------------------------------------------


def flags(*items):
    return ai_news.NewsFlags(flags=[ai_news.NewsFlag(**i) for i in items])


def test_news_scan_extracts_and_stores_flags(ctx):
    parsed = flags(
        {
            "player": "Quinn Hughes",
            "status": "out",
            "note": "Lower-body injury, out a week",
            "source_url": "https://nhl.com/a",
        },
        {"player": "Somebody Else", "status": "role_up", "note": "Moved to PP1"},
    )
    client = fake_client([message("end_turn", text("Quinn Hughes: out a week (nhl.com)"))], parsed)
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    news = ai_news.scan(ctx, client, now=now)
    first = client.sdk.requests[0]
    assert "Quinn Hughes" in first["messages"][0]["content"] and first["tools"][0]["name"] == "web_search"
    assert "Quinn Hughes: out a week" in client.sdk.parse_requests[0]["messages"][0]["content"]
    assert news.flags[0]["fantrax_id"] and news.flags[1]["fantrax_id"] is None

    assert ai_news.fresh(ctx.conn, 6, now + timedelta(hours=5)).flags == news.flags
    assert ai_news.fresh(ctx.conn, 6, now + timedelta(hours=7)) is None
    assert [f["player"] for f in ai_news.outs(news)] == ["Quinn Hughes"]
    assert ai_news.outs(news, play=["quinn hughes"]) == []


def test_news_needs_web_search(ctx):
    with pytest.raises(AiUnavailable):
        ai_news.scan(ctx, fake_client([], web_search=False))


def test_lineup_benches_news_outs(conn, settings):
    full_sync(conn, settings)
    from hockey.sources.nhl import NhlClient

    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    v = Valuer(conn, load_rules(conn), settings.league)
    base = build_report(v, MINE, P2, games)
    assert "Quinn Hughes" in {w.row.name for w in base.starters}
    r = build_report(v, MINE, P2, games, news_outs={"Quinn Hughes": "lower-body injury"})
    qh = next(w for w in r.bench + r.others if w.row.name == "Quinn Hughes")
    assert qh.avail.out and any("AI news" in f for f in qh.avail.flags)


def test_report_renders_ai_section():
    news = ai_news.News(
        datetime(2026, 10, 3, 12, tzinfo=UTC),
        [{"player": "Quinn Hughes", "status": "out", "note": "injured", "source_url": "https://nhl.com/a"}],
    )
    md = render("TBB", datetime(2026, 10, 3, 12, tzinfo=UTC), ai_take="- Start Demko.", news=news)
    assert "## AI take" in md and "- Start Demko." in md and "[link](https://nhl.com/a)" in md
    assert "## AI take" not in render("TBB", datetime(2026, 10, 3, 12, tzinfo=UTC))


# --- CLI ------------------------------------------------------------------------------------------


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("hockey.config.load_dotenv", lambda *a, **k: None)


def test_cli_ai_off_without_key(cli_db, no_key):  # noqa: F811
    for args in (["ask", "who do I start?"], ["news"]):
        out = cli_db.invoke(cli.app, args)
        assert out.exit_code == 0 and "ANTHROPIC_API_KEY" in out.output, out.output
    lu = cli_db.invoke(cli.app, ["lineup", "--period", "2", "--ai"], env={"COLUMNS": "200"})
    assert lu.exit_code == 0 and "continuing without AI news" in lu.output and "Recommended" in lu.output


def _store_news(tmp_path):
    from hockey import db

    c = db.connect(tmp_path / "r.db")
    ai_news.save(
        c,
        ai_news.News(
            datetime.now(UTC),
            [
                {
                    "player": "Quinn Hughes",
                    "status": "out",
                    "note": "lower-body injury",
                    "source_url": "https://nhl.com/a",
                }
            ],
        ),
    )
    c.close()


def test_cli_lineup_uses_stored_news(cli_db, no_key, tmp_path):  # noqa: F811
    _store_news(tmp_path)
    out = cli_db.invoke(cli.app, ["lineup", "--period", "2", "--ai"], env={"COLUMNS": "200"})
    assert out.exit_code == 0, out.output
    assert "Benched as out per AI news: Quinn Hughes" in out.output and "lower-body injury" in out.output
    kept = cli_db.invoke(
        cli.app, ["lineup", "--period", "2", "--ai", "--play", "Quinn Hughes"], env={"COLUMNS": "200"}
    )
    assert kept.exit_code == 0 and "Benched as out per AI news" not in kept.output


def test_cli_report_ai_without_key_keeps_news(cli_db, no_key, tmp_path):  # noqa: F811
    _store_news(tmp_path)
    ok = cli_db.invoke(
        cli.app, ["report", "--ai", "--out-dir", str(tmp_path / "rep")], env={"COLUMNS": "200"}
    )
    assert ok.exit_code == 0, ok.output
    md = next((tmp_path / "rep").glob("*.md")).read_text()
    assert "## AI take" in md and "Quinn Hughes" in md and "## Lineup" in md


def test_cli_ask_with_fake_client(cli_db, monkeypatch):  # noqa: F811
    client = fake_client(
        [
            message("tool_use", tool_use("get_roster", {})),
            message("end_turn", text("Your roster looks strong; start Demko.")),
        ]
    )
    monkeypatch.setattr("hockey.ai.client.make_client", lambda league, **kw: client)
    out = cli_db.invoke(cli.app, ["ask", "how is my team?", "--no-web"], env={"COLUMNS": "200"})
    assert out.exit_code == 0, out.output
    assert "start Demko" in out.output and "get_roster" in out.output
    assert "MoneyPuck.com" in out.output and "Recommendations only" in out.output
    assert "tools" in client.sdk.requests[0] and all(
        t.get("type") != "web_search_20260209" for t in client.sdk.requests[0]["tools"]
    )
    system = client.sdk.requests[0]["system"][0]["text"]
    assert "The Blue Blazers" in system
