"""The local browser UI, in-process (FastAPI TestClient: no sockets, no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from hockey import db
from hockey.ai import news as ai_news
from hockey.sources.nhl import NhlClient
from hockey.web.app import HOST, build_server, create_app
from tests.conftest import FakeHttp
from tests.test_ai import fake_client, message, text, tool_use
from tests.test_lineup import P2
from tests.test_sync_csv import full_sync


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def web(settings, monkeypatch, no_key):
    conn = db.connect(settings.db_path, check_same_thread=False)
    full_sync(conn, settings)
    games = NhlClient(FakeHttp(), date(2026, 10, 1)).schedule(P2.start, P2.end)
    monkeypatch.setattr(NhlClient, "schedule", lambda self, s, e: games)
    app = create_app(settings, conn, FakeHttp())
    with TestClient(app) as client:
        client.conn = conn
        yield client
    conn.close()


def ok(client, url, *texts):
    r = client.get(url)
    assert r.status_code == 200, url
    assert 'class="banner error"' not in r.text, (url, r.text[:3000])
    for t in texts:
        assert t in r.text, (url, t)
    return r.text


def test_every_page_renders(web):
    ok(web, "/", "This week", "recommended FP", "Pickups", "You rank", "Recommendations only")
    ok(web, "/lineup?period=2", "Lineup for period 2", "Recommended actives", "Quinn Hughes")
    ok(web, "/waivers?period=2", "Best pickups", "By position", "Streamers for period 2")
    ok(web, "/rank?pos=D&available=true", "Rankings", "FA")
    ok(web, "/roster", "The Blue Blazers", "Quinn Hughes", "Total")
    ok(web, "/roster?team=Team 03", "Team 03")
    ok(web, "/player/McDavid", "Connor McDavid", "Per-game rates", "FP per game")
    ok(web, "/keepers", "Keeper plan", "Keeper value")
    ok(web, "/intel", "Trade partners", "Thatcher Demko", "analyze")
    ok(web, "/trade", "Trade analyzer", "<datalist")
    ok(web, "/news", "News")


def test_moneypuck_credit_on_pages_that_use_it(web):
    ok(web, "/lineup?period=2", "MoneyPuck.com")
    ok(web, "/intel", "MoneyPuck.com")
    assert "MoneyPuck.com" not in ok(web, "/ask")


def test_lineup_out_and_ai_news(web):
    page = ok(web, "/lineup?period=2&out=Quinn%20Hughes")
    bench = page.split("Bench, IR and Minors")[1]
    assert "Quinn Hughes" in bench and "marked out (--out)" in bench
    ai_news.save(
        web.conn,
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
    page = ok(web, "/lineup?period=2&ai=true")
    assert "out per AI news: lower-body injury" in page and "AI news for your roster" in page
    played = ok(web, "/lineup?period=2&ai=true&play=Quinn%20Hughes")
    assert "out per AI news" not in played


def test_trade_page(web):
    page = ok(web, "/trade?give=Stutzle&get=Demko", "Impact", "Thatcher Demko", "Score")
    assert 'name="get" list="others" value="Demko"' in page  # the form keeps what you typed
    assert any(v in page for v in ("Good for", "Bad for"))
    amb = web.get("/trade?give=Pettersson&get=Demko").text
    assert "banner error" in amb and "ambiguous" in amb and 'value="Pettersson"' in amb  # form kept


def test_errors_are_banners_not_500s(web, settings, tmp_path):
    assert "No matching roster period" in web.get("/lineup?period=999").text
    assert "No player matching" in web.get("/player/Nobody%20Atall").text
    empty = create_app(settings, db.connect(tmp_path / "empty.db", check_same_thread=False), FakeHttp())
    with TestClient(empty) as c:
        r = c.get("/")
        assert r.status_code == 200 and "banner error" in r.text and "Refresh data now" in r.text


def test_refresh_data_runs_the_guarded_sync(settings, tmp_path, no_key):
    conn = db.connect(tmp_path / "fresh.db", check_same_thread=False)
    with TestClient(create_app(settings, conn, FakeHttp())) as c:
        body = c.post("/api/sync").json()
        assert "error" not in body and body["counts"]
        assert c.app.state.hub.last_sync() is not None


def test_ai_parts_show_the_hint_without_a_key(web):
    ok(web, "/ask", "ANTHROPIC_API_KEY")
    ok(web, "/", "ANTHROPIC_API_KEY")
    events = web.post("/api/ask", json={"message": "hi"}).text
    assert '"type": "error"' in events and "ANTHROPIC_API_KEY" in events
    assert "ANTHROPIC_API_KEY" in web.post("/api/news").json()["error"]


def sse(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.split("\n\n") if line.startswith("data: ")]


def test_chat_streams_tools_and_text(web, monkeypatch):
    client = fake_client(
        [
            message("tool_use", tool_use("best_lineup", {"period": 2})),
            message("end_turn", text("Start **Demko** this week.")),
            message("end_turn", text("Hughes stays on the bench.")),
        ]
    )
    monkeypatch.setattr("hockey.ai.client.make_client", lambda league, **kw: client)
    events = sse(web.post("/api/ask", json={"message": "Who do I start?", "web": False}).text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session" and "tool" in kinds and kinds[-1] == "done", events
    assert next(e for e in events if e["type"] == "tool")["name"] == "best_lineup"
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Start **Demko** this week."
    done = events[-1]
    assert done["credit"] and "MoneyPuck" in done["credit"]
    assert all(t.get("type") != "web_search_20260209" for t in client.sdk.requests[0]["tools"])

    # Follow-up in the same session carries the history.
    sid = events[0]["session"]
    events = sse(web.post("/api/ask", json={"message": "And Hughes?", "session": sid, "web": False}).text)
    assert events[-1]["type"] == "done"
    history = client.sdk.requests[-1]["messages"]
    assert history[0]["content"].endswith("Who do I start?") and history[-1]["content"] == "And Hughes?"


def test_failed_chat_turn_is_dropped(web, monkeypatch):
    client = fake_client([message("refusal"), message("end_turn", text("ok"))])
    monkeypatch.setattr("hockey.ai.client.make_client", lambda league, **kw: client)
    first = sse(web.post("/api/ask", json={"message": "q1", "web": False}).text)
    assert first[-1]["type"] == "error" and "declined" in first[-1]["message"]
    sid = first[0]["session"]
    second = sse(web.post("/api/ask", json={"message": "q2", "session": sid, "web": False}).text)
    assert second[-1]["type"] == "done"
    assert [m["role"] for m in client.sdk.requests[-1]["messages"]] == ["user"]


def test_ai_take_on_dashboard(web, monkeypatch):
    client = fake_client([message("end_turn", text("- Start Demko."))])
    monkeypatch.setattr("hockey.ai.client.make_client", lambda league, **kw: client)
    assert web.post("/api/take").json() == {"text": "- Start Demko."}
    assert "## Lineup" in client.sdk.requests[0]["messages"][0]["content"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # so the dashboard shows the AI card
    ok(web, "/", "- Start Demko.", "Refresh AI take")


def test_server_binds_localhost_only(web):
    server = build_server(8765, web.app)
    assert HOST == "127.0.0.1" and server.config.host == "127.0.0.1"
