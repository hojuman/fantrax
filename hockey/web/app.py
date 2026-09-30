"""Local browser UI: `hockey web`. Same engine as the CLI, rendered as pages.

Runs on 127.0.0.1 only. Pages read the local database through `LeagueContext` (no new math). The
only actions are "Refresh data" (the same guarded, GET-only sync as `hockey sync`), "Check news" and
the AI chat. Nothing here can act on Fantrax.

One SQLite connection is shared across the server's threads and serialized with a lock (this is a
single-user app); the Valuer is rebuilt only when the synced data changes.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from rich.text import Text

from hockey import db
from hockey.ai.client import AiError, AiUnavailable
from hockey.config import ConfigError, Settings, load_settings
from hockey.context import ContextError, LeagueContext, choose_period, my_team, team_by_name
from hockey.db import get_meta
from hockey.http import FetchError, HttpClient
from hockey.idmap.normalize import basic
from hockey.lineup.periods import parse_periods
from hockey.sources.moneypuck import CREDIT
from hockey.views import tables

HOST = "127.0.0.1"  # never 0.0.0.0: this UI is for the machine it runs on
DEFAULT_PORT = 8765
HERE = Path(__file__).parent
STAMP_SQL = "SELECT MAX(updated_at) FROM meta WHERE key != 'ai_news'"
POSITIONS = ("C", "LW", "RW", "D", "G", "F")
NAV = [
    ("/", "Dashboard"),
    ("/lineup", "Lineup"),
    ("/waivers", "Waivers"),
    ("/rank", "Rankings"),
    ("/roster", "Rosters"),
    ("/trade", "Trade"),
    ("/keepers", "Keepers"),
    ("/intel", "League"),
    ("/news", "News"),
    ("/ask", "Ask AI"),
]
PAGE_ERRORS = (ContextError, FetchError, AiError, AiUnavailable, ConfigError)


# --- shared state ---------------------------------------------------------------------------------


class Hub:
    """The one db connection, HTTP client and cached LeagueContext, behind a lock."""

    def __init__(self, settings: Settings, conn: sqlite3.Connection, http: Any):
        self.settings, self.conn, self.http = settings, conn, http
        self.lock = threading.RLock()
        self._ctx: LeagueContext | None = None
        self._stamp: object = None
        self.take: tuple[datetime, str] | None = None  # last AI take (dashboard)
        self.chats: dict[str, dict] = {}

    def ctx(self) -> LeagueContext:
        stamp = self.conn.execute(STAMP_SQL).fetchone()[0]
        if self._ctx is None or stamp != self._stamp:
            self._ctx = LeagueContext(self.settings, self.conn, self.http, my_team(self.conn, self.settings))
            self._stamp = stamp
        return self._ctx

    def invalidate(self) -> None:
        self._ctx = None

    def last_sync(self) -> datetime | None:
        stamp = self.conn.execute(STAMP_SQL).fetchone()[0]
        return datetime.fromtimestamp(stamp, UTC) if stamp else None

    def periods(self):
        return parse_periods(get_meta(self.conn, "roster_periods") or [])

    def team_names(self) -> list[str]:
        return [r["name"] for r in self.conn.execute("SELECT name FROM fantasy_team ORDER BY name")]


def ai_hint(league: dict) -> str | None:
    """None when the AI layer is usable, else the same one-line fix the CLI prints."""
    from hockey.ai.client import make_client

    try:
        import pydantic  # noqa: F401  (hockey.ai.news needs it)

        make_client(league)
    except ImportError:
        return "AI layer off: install it with `uv sync --extra ai`"
    except AiUnavailable as e:
        return str(e)
    return None


def news_module():
    try:
        from hockey.ai import news
    except ImportError:
        return None
    return news


# --- template helpers -----------------------------------------------------------------------------


def plain(markup: str | None) -> str:
    """Rich markup (the terminal views' helpers use it) -> plain text."""
    return Text.from_markup(markup or "").plain


def fmt(x: float | None, spec: str = ".1f") -> str:
    return "–" if x is None else format(x, spec)


def signed(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "–"
    s = f"{x:+.{nd}f}"
    return s.replace("-0", "0") if float(s) == 0 else s


def local(dt: datetime | None, spec: str = "%a %b %d %H:%M") -> str:
    return dt.astimezone().strftime(spec) if dt else ""


def games_cell(w) -> str:
    return tables._games_cell(w)


def why_not_starting(w) -> str:
    if w.avail.out:
        return w.avail.flags[0]
    if w.status in ("INJURED_RESERVE", "MINORS"):
        return "IR slot" if w.status == "INJURED_RESERVE" else "Minors slot"
    if w.avail.games == 0:
        return "no games"
    return "lower projected value"


def rank_class(rank: int, n: int) -> str:
    cut = max(1, round(n * 0.3))
    return "good" if rank <= cut else "bad" if rank > n - cut else ""


# --- the app --------------------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None, conn: sqlite3.Connection | None = None, http: Any = None
) -> FastAPI:
    settings = settings or load_settings()
    conn = conn or db.connect(settings.db_path, check_same_thread=False)
    hub = Hub(settings, conn, http or HttpClient(conn))
    app = FastAPI(title="Hockey assistant", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(plain=plain, fmt=fmt, signed=signed, local=local)
    env.globals.update(
        basis=lambda p: plain(tables.basis(p)),
        components=lambda r: plain(tables._components(r)),
        games_cell=games_cell,
        why_not_starting=why_not_starting,
        rank_class=rank_class,
        STAT_LABELS=tables.STAT_LABELS,
        CREDIT=CREDIT,
        NAV=NAV,
    )

    def render(request: Request, name: str, status: int = 200, **data) -> HTMLResponse:
        data.setdefault("credit", False)
        data.setdefault("title", "")
        data["team"] = settings.my_team_name
        data["path"] = request.url.path
        return templates.TemplateResponse(request, name, data, status_code=status)

    def page(request: Request, name: str, build) -> HTMLResponse:
        """Build a page's data under the lock; known errors become a friendly banner."""
        with hub.lock:
            try:
                data = build(hub.ctx())
            except PAGE_ERRORS as e:
                return render(request, "error.html", status=200, error=str(e), title="Something's missing")
        return render(request, name, **data)

    # ---- pages ----

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        def build(ctx):
            per = choose_period(ctx.conn)
            lineup = ctx.lineup(per)
            waivers = ctx.waivers(per, limit=5)
            league, iper = ctx.intel()
            mod = news_module()
            news = mod.load(ctx.conn) if mod else None
            swaps = sorted((s for _, ss in league.partners for s in ss), key=lambda s: -s.mutual)[:3]
            return {
                "title": "Dashboard",
                "lineup": lineup,
                "waivers": waivers,
                "league": league,
                "swaps": swaps,
                "news": news,
                "take": hub.take,
                "ai_hint": ai_hint(settings.league),
                "last_sync": hub.last_sync(),
                "now": datetime.now(UTC),
                "credit": lineup.uses_moneypuck or waivers.uses_moneypuck or league.uses_moneypuck,
            }

        return page(request, "dashboard.html", build)

    @app.get("/lineup", response_class=HTMLResponse)
    def lineup(
        request: Request,
        period: int | None = None,
        out: list[str] = Query(default=[]),
        ai: bool = False,
        play: list[str] = Query(default=[]),
    ):
        def build(ctx):
            per = choose_period(ctx.conn, period)
            mod = news_module()
            news = mod.load(ctx.conn) if (ai and mod) else None
            news_outs = {f["player"]: f["note"] for f in mod.outs(news, play)} if news else {}
            r = ctx.lineup(per, out, news_outs)
            names = {w.row.name for w in r.starters + r.bench + r.others}
            keys = {basic(n) for n in names}
            flags = [f for f in news.flags if basic(f["player"]) in keys] if news else []
            return {
                "title": f"Lineup · period {per.number}",
                "r": r,
                "periods": hub.periods(),
                "out": set(out),
                "ai": ai,
                "play": set(play),
                "news": news,
                "news_flags": flags,
                "news_outs": news_outs,
                "roster": sorted(names),
                "now": datetime.now(UTC),
                "credit": r.uses_moneypuck,
            }

        return page(request, "lineup.html", build)

    @app.get("/waivers", response_class=HTMLResponse)
    def waivers(
        request: Request,
        pos: str | None = None,
        period: int | None = None,
        protect: list[str] = Query(default=[]),
        max_ros_cost: float = 10.0,
        limit: int = 10,
    ):
        def build(ctx):
            per = choose_period(ctx.conn, period)
            p = pos.upper() if pos else None
            if p and p not in ("C", "LW", "RW", "D", "G"):
                raise ContextError("Position must be one of C, LW, RW, D, G")
            r = ctx.waivers(per, protect=protect, pos=p, limit=limit, max_ros_cost=max_ros_cost)
            mine = ctx.valuer.team_roster(ctx.me["team_id"])
            return {
                "title": "Waivers",
                "r": r,
                "periods": hub.periods(),
                "pos": p or "",
                "protect": set(protect),
                "max_ros_cost": max_ros_cost,
                "mine": sorted(x.name for x in mine if (x.status or "").upper() in ("ACTIVE", "RESERVE")),
                "credit": r.uses_moneypuck,
            }

        return page(request, "waivers.html", build)

    @app.get("/rank", response_class=HTMLResponse)
    def rank(
        request: Request,
        pos: str | None = None,
        available: bool = False,
        owner: str | None = None,
        sort: str = "ros",
        limit: int = 50,
    ):
        def build(ctx):
            rows = [r for r in ctx.valuer.league_players() if r.projection]
            p = (pos or "").upper()
            if p == "F":
                rows = [r for r in rows if r.pos_group == "F"]
            elif p:
                rows = [r for r in rows if p in {x.strip().upper() for x in r.positions.split(",")}]
            if available:
                rows = [r for r in rows if r.owner in ("FA", "W")]
            if owner:
                t = team_by_name(ctx.conn, owner)
                if t is None:
                    raise ContextError(f"No fantasy team {owner!r}")
                rows = [r for r in rows if r.owner == t["name"]]
            key = (lambda r: r.projection.ros_fp) if sort == "ros" else (lambda r: r.projection.fp_per_gp)
            rows = sorted(rows, key=key, reverse=True)[: max(1, min(limit, 500))]
            return {
                "title": "Rankings",
                "rows": rows,
                "pos": p,
                "available": available,
                "owner": owner or "",
                "sort": sort,
                "limit": limit,
                "teams": hub.team_names(),
                "positions": POSITIONS,
                "credit": any(r.projection.uses_moneypuck for r in rows),
            }

        return page(request, "rank.html", build)

    @app.get("/player/{key}", response_class=HTMLResponse)
    def player(request: Request, key: str):
        def build(ctx):
            matches = ctx.valuer.find(key)
            if not matches:
                raise ContextError(f"No player matching {key!r}")
            if len(matches) > 1:
                return {"title": f"Players matching {key}", "matches": matches[:30], "r": None}
            r = matches[0]
            p = r.projection
            rows = []
            if p:
                weights = ctx.valuer.rules.for_pos_group(p.pos_group)
                stats = [s for s in weights if weights[s]]
                extra = ["toi_min"] + (["sa", "sv_pct"] if p.pos_group == "G" else [])
                for stat in stats + [s for s in extra if s not in stats]:
                    rows.append(_stat_row(p, stat, weights, stat in stats))
            return {
                "title": r.name,
                "r": r,
                "p": p,
                "stat_rows": rows,
                "matches": [],
                "credit": bool(p and p.uses_moneypuck),
            }

        return page(request, "player.html", build)

    @app.get("/roster", response_class=HTMLResponse)
    def roster(request: Request, team: str | None = None):
        def build(ctx):
            row = team_by_name(ctx.conn, team) if team else ctx.me
            if row is None:
                raise ContextError(f"No fantasy team {team!r}")
            rows = ctx.valuer.team_roster(row["team_id"])

            def key(r):
                st = tables.STATUS_ORDER.get((r.status or "").upper(), 1)
                return (
                    st,
                    tables.POS_ORDER.get(r.pos_group or "", 3),
                    -(r.projection.ros_fp if r.projection else -1e9),
                )

            rows = sorted(rows, key=key)
            return {
                "title": row["name"],
                "team_name": row["name"],
                "rows": rows,
                "teams": hub.team_names(),
                "total": sum(r.projection.ros_fp for r in rows if r.projection),
                "credit": any(r.projection and r.projection.uses_moneypuck for r in rows),
            }

        return page(request, "roster.html", build)

    @app.get("/trade", response_class=HTMLResponse)
    def trade(
        request: Request,
        give: str = "",
        get: str = "",
        give_pick: list[str] = Query(default=[]),
        get_pick: list[str] = Query(default=[]),
        partner: str = "",
        keeper_weight: float = 0.5,
    ):
        def build(ctx):
            pool = ctx.valuer.league_players()
            mine = sorted(r.name for r in pool if r.owner == ctx.me["name"])
            others = sorted(r.name for r in pool if r.owner not in (ctx.me["name"], "FA", "W", None))
            form = {
                "give": give,
                "get": get,
                "give_pick": ", ".join(give_pick),
                "get_pick": ", ".join(get_pick),
                "partner": partner,
                "keeper_weight": keeper_weight,
            }
            data = {
                "title": "Trade",
                "form": form,
                "mine": mine,
                "others": others,
                "teams": hub.team_names(),
                "t": None,
            }
            gp = [p.strip() for x in give_pick for p in x.split(",") if p.strip()]
            tp = [p.strip() for x in get_pick for p in x.split(",") if p.strip()]
            if (give or gp) and (get or tp):
                try:
                    t = ctx.trade(
                        give or "-",
                        get or "-",
                        give_picks=gp,
                        get_picks=tp,
                        partner=partner or None,
                        keeper_weight=keeper_weight,
                    )
                except ContextError as e:
                    data["error"] = str(e)
                else:
                    data |= {"t": t, "credit": t.uses_moneypuck}
            return data

        return page(request, "trade.html", build)

    @app.get("/keepers", response_class=HTMLResponse)
    def keepers(request: Request, horizon: int = 3):
        def build(ctx):
            p = ctx.keeper_plan(max(1, min(horizon, 5)))
            order = {"regular+tag": 0, "regular": 1, "minors": 2, "unknown": 3, "release": 4}
            return {
                "title": "Keepers",
                "p": p,
                "horizon": horizon,
                "cands": sorted(p.candidates, key=lambda c: (order[c.choice], -c.value)),
                "tags": [c.row.name for c in p.candidates if c.choice == "regular+tag"],
                "credit": p.uses_moneypuck,
            }

        return page(request, "keepers.html", build)

    @app.get("/intel", response_class=HTMLResponse)
    def intel(request: Request):
        def build(ctx):
            league, per = ctx.intel()
            return {
                "title": "League",
                "league": league,
                "period": per,
                "slots": list(league.me.by_slot),
                "n": len(league.teams),
                "credit": league.uses_moneypuck,
            }

        return page(request, "intel.html", build)

    @app.get("/news", response_class=HTMLResponse)
    def news(request: Request):
        def build(ctx):
            mod = news_module()
            n = mod.load(ctx.conn) if mod else None
            mine = {r.name for r in ctx.valuer.team_roster(ctx.me["team_id"])}
            return {"title": "News", "news": n, "mine": mine, "ai_hint": ai_hint(settings.league)}

        return page(request, "news.html", build)

    @app.get("/ask", response_class=HTMLResponse)
    def ask(request: Request):
        return render(request, "ask.html", title="Ask AI", ai_hint=ai_hint(settings.league))

    # ---- actions (JSON) ----

    @app.post("/api/sync")
    def api_sync():
        from hockey.sources.fantrax_fxea import FantraxError, FantraxShapeError
        from hockey.sync import SyncReport, run_idmap, sync_fantrax, sync_moneypuck, sync_nhl

        report = SyncReport()
        with hub.lock:
            try:
                sync_fantrax(conn, hub.http, settings, report)
                sync_nhl(conn, hub.http, date.today(), report)
                sync_moneypuck(conn, hub.http, date.today(), report)
                run_idmap(conn, report)
            except (ConfigError, FetchError, FantraxError, FantraxShapeError) as e:
                return JSONResponse({"error": str(e)})
            finally:
                hub.invalidate()
        return {
            "counts": {k: str(v) for k, v in report.counts.items()},
            "notes": report.notes,
            "problems": report.problems,
        }

    @app.post("/api/news")
    def api_news():
        from hockey.ai.client import make_client

        mod = news_module()
        if mod is None:
            return JSONResponse({"error": "AI layer off: install it with `uv sync --extra ai`"})
        try:
            client = make_client(settings.league)
            with hub.lock:
                n = mod.scan(hub.ctx(), client)
        except PAGE_ERRORS as e:
            return JSONResponse({"error": str(e)})
        return {"flags": len(n.flags), "dropped_domains": client.dropped_domains}

    @app.post("/api/take")
    def api_take():
        from hockey.ai.client import make_client
        from hockey.ai.take import report_take
        from hockey.report.daily import render as render_md

        try:
            client = make_client(settings.league)
            with hub.lock:
                ctx = hub.ctx()
                per = choose_period(ctx.conn)
                mod = news_module()
                news = mod.load(ctx.conn) if mod else None
                league, _ = ctx.intel()
                md = render_md(
                    ctx.me["name"],
                    datetime.now(UTC),
                    lineup=ctx.lineup(per),
                    waivers=ctx.waivers(per),
                    league=league,
                )
            ans = report_take(ctx, client, md, news)  # rules/league only: no db access
        except (*PAGE_ERRORS, ImportError) as e:
            return JSONResponse({"error": str(e)})
        hub.take = (datetime.now(UTC), ans.text)
        return {"text": ans.text}

    @app.post("/api/ask")
    def api_ask(body: dict = Body(...)):
        return _ask_stream(hub, settings, body)

    return app


def _stat_row(p, stat: str, weights: dict, scored: bool) -> dict:
    def window_rate(w):
        if w is None or not w.gp:
            return None
        if stat == "sv_pct":
            return w.stats.get("sv", 0.0) / w.stats["sa"] if w.stats.get("sa") else None
        return w.rate(stat)

    prior = p.prior_rates.get(stat)
    if stat == "sv_pct" and p.prior_rates.get("sa"):
        prior = p.prior_rates.get("sv", 0.0) / p.prior_rates["sa"]
    spec = ".3f" if stat == "sv_pct" else ".2f"
    return {
        "label": tables.STAT_LABELS.get(stat, stat),
        "scored": scored,
        "weight": f"{weights[stat]:g}" if scored else "",
        "prior": fmt(prior, spec),
        "season": fmt(window_rate(p.season), spec),
        "last30": fmt(window_rate(p.last30), spec),
        "last14": fmt(window_rate(p.last14), spec),
        "proj": fmt(p.rates.get(stat), spec),
        "prior_wt": f"{p.prior_weight[stat]:.0%}" if stat in p.prior_weight else "",
        "fp": f"{p.breakdown.parts.get(stat, 0.0):+.2f}" if scored else "",
    }


def _sse(item: dict) -> str:
    return f"data: {json.dumps(item, default=str)}\n\n"


def _ask_stream(hub: Hub, settings: Settings, body: dict) -> StreamingResponse:
    """Server-sent events: text deltas, tool calls, then a final `done` (or `error`)."""
    from hockey.ai.agent import converse
    from hockey.ai.client import make_client
    from hockey.ai.prompts import system_blocks
    from hockey.ai.tools import Executor

    question = str(body.get("message") or "").strip()
    sid = str(body.get("session") or uuid.uuid4())
    web = bool(body.get("web", True))
    events: queue.Queue = queue.Queue()

    def fail(msg: str) -> StreamingResponse:
        return StreamingResponse(
            iter([_sse({"type": "error", "message": msg})]), media_type="text/event-stream"
        )

    if not question:
        return fail("Type a question first.")
    try:
        client = make_client(settings.league, web_search=None if web else False)
        with hub.lock:
            ctx = hub.ctx()
            system = system_blocks(ctx.league, ctx.valuer.rules, f"The manager's team is {ctx.me['name']}.")
    except PAGE_ERRORS as e:
        return fail(str(e))

    class LockedExecutor(Executor):
        def run(self, name, args):
            with hub.lock:
                return super().run(name, args)

    chat = hub.chats.setdefault(sid, {"messages": []})
    chat.setdefault("executor", LockedExecutor(ctx))
    messages: list[dict] = chat["messages"]
    if not messages:
        question = f"(Today is {date.today():%A %Y-%m-%d}.)\n\n{question}"
    start = len(messages)
    messages.append({"role": "user", "content": question})

    def tool_detail(name: str, args: dict) -> str:
        if name == "web_search":
            return str(args.get("query") or "")
        return ", ".join(f"{k}={v}" for k, v in args.items())

    def work():
        try:
            ans = converse(
                client,
                system,
                messages,
                chat["executor"],
                web=web and client.settings.web_search,
                on_text=lambda t: events.put({"type": "text", "text": t}),
                on_tool=lambda n, a: events.put({"type": "tool", "name": n, "detail": tool_detail(n, a)}),
            )
            events.put(
                {
                    "type": "done",
                    "session": sid,
                    "sources": ans.sources[:10],
                    "credit": CREDIT if chat["executor"].uses_moneypuck else None,
                    "truncated": ans.truncated,
                    "tokens": {"in": ans.input_tokens + ans.cache_read_tokens, "out": ans.output_tokens},
                    "searches": ans.searches,
                    "dropped_domains": client.dropped_domains,
                }
            )
        except Exception as e:  # the stream must always end with a message the page can show
            del messages[start:]  # drop the failed turn so the next question starts clean
            msg = str(e) if isinstance(e, AiError) else f"Unexpected error: {e!r}"
            events.put({"type": "error", "message": msg})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        yield _sse({"type": "session", "session": sid})
        while (item := events.get()) is not None:
            yield _sse(item)

    return StreamingResponse(stream(), media_type="text/event-stream")


def build_server(port: int = DEFAULT_PORT, app: FastAPI | None = None):
    """A uvicorn server bound to 127.0.0.1 only."""
    import uvicorn

    return uvicorn.Server(uvicorn.Config(app or create_app(), host=HOST, port=port, log_level="warning"))


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """Run the UI and open it in the default browser."""
    server = build_server(port)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{port}/")).start()
    server.run()
