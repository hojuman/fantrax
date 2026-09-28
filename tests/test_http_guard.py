import time

import pytest
from pytest_socket import SocketBlockedError

from hockey.http import HttpClient, ReadOnlyViolation, Response, check_allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://www.fantrax.com/fxea/general/getPlayerIds?sport=NHL",
        "https://api.nhle.com/stats/rest/en/skater/summary",
        "https://api-web.nhle.com/v1/schedule/now",
        "https://moneypuck.com/data.htm",
    ],
)
def test_allowed(url):
    check_allowed(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.fantrax.com/fxpa/req?leagueId=x",  # private web-app backend (ToS)
        "https://www.fantrax.com/fantasy/league/x/team/roster",  # HTML pages (scraping)
        "https://www.fantrax.com/newui/fantasy/transactions.go",
        "http://www.fantrax.com/fxea/general/getPlayerIds",  # not https
        "https://evil.example.com/fxea/general/getPlayerIds",
        "https://fantrax.com.evil.example/fxea/",
    ],
)
def test_refused(url):
    with pytest.raises(ReadOnlyViolation):
        check_allowed(url)


def test_client_is_get_only(conn):
    client = HttpClient(conn)
    for verb in ("post", "put", "patch", "delete", "request"):
        assert not hasattr(client, verb)


def test_guard_runs_before_any_network(conn):
    with pytest.raises(ReadOnlyViolation):
        HttpClient(conn).get("https://www.fantrax.com/fxpa/req")


def test_cache_hit_needs_no_network(conn):
    url = "https://api-web.nhle.com/v1/schedule/now"
    conn.execute("INSERT INTO http_cache VALUES (?,?,?,?,?)", (url, time.time(), 3600, 200, '{"ok": 1}'))
    client = HttpClient(conn)
    r = client.get(url)
    assert r.from_cache and r.json() == {"ok": 1}
    assert client.stats == {"hits": 1, "fetches": 0}


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_expired_cache_refetches(conn):
    url = "https://api-web.nhle.com/v1/schedule/now"
    conn.execute("INSERT INTO http_cache VALUES (?,?,?,?,?)", (url, time.time() - 7200, 3600, 200, "{}"))
    with pytest.raises(SocketBlockedError):  # tried the network: proves expiry and that tests are offline
        HttpClient(conn, max_attempts=1).get(url)


def test_forever_ttl_never_expires(conn):
    url = "https://api.nhle.com/stats/rest/en/skater/summary?x=1"
    conn.execute("INSERT INTO http_cache VALUES (?,?,?,?,?)", (url, 0.0, None, 200, "{}"))
    assert HttpClient(conn).get(url, ttl=None).from_cache


def test_error_bodies_not_cached(conn, monkeypatch):
    client = HttpClient(conn)
    monkeypatch.setattr(client, "_fetch", lambda u: Response(u, 200, '{"error": "private"}', False))
    url = "https://www.fantrax.com/fxea/general/getLeagueInfo?leagueId=x"
    client.get(url, cache_if=lambda r: "error" not in r.json())
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


def test_refresh_bypasses_cache(conn, monkeypatch):
    url = "https://api-web.nhle.com/v1/schedule/now"
    conn.execute("INSERT INTO http_cache VALUES (?,?,?,?,?)", (url, time.time(), 3600, 200, '{"old": 1}'))
    client = HttpClient(conn, refresh=True)
    monkeypatch.setattr(client, "_fetch", lambda u: Response(u, 200, '{"new": 1}', False))
    assert client.get(url).json() == {"new": 1}


def _client_with(conn, handler):
    import httpx

    c = HttpClient(conn, transport=httpx.MockTransport(handler), max_attempts=1)
    c._throttle = lambda host: None  # no sleeping in tests
    return c


def test_same_host_redirect_followed_and_cached_under_original_url(conn):
    import httpx

    def handler(req):
        if req.url.path == "/v1/roster/VAN/current":
            return httpx.Response(307, headers={"location": "/v1/roster/VAN/20262027"})
        return httpx.Response(200, json={"forwards": []})

    url = "https://api-web.nhle.com/v1/roster/VAN/current"
    r = _client_with(conn, handler).get(url)
    assert r.status == 200 and r.url == url and r.json() == {"forwards": []}
    assert conn.execute("SELECT url FROM http_cache").fetchone()[0] == url


@pytest.mark.parametrize(
    "location",
    [
        "https://www.fantrax.com/fxpa/req",  # allowed host, forbidden path
        "https://evil.example.com/x",  # off the allowlist
        "https://api.nhle.com/stats/rest/en/x",  # allowlisted, but a different host
    ],
)
def test_redirect_off_allowlist_refused(conn, location):
    import httpx

    handler = lambda req: httpx.Response(302, headers={"location": location})  # noqa: E731
    with pytest.raises(ReadOnlyViolation):
        _client_with(conn, handler).get("https://api-web.nhle.com/v1/schedule/now")


def test_fantrax_redirect_to_web_app_refused(conn):
    import httpx

    handler = lambda req: httpx.Response(302, headers={"location": "/fantasy/league/x/home"})  # noqa: E731
    with pytest.raises(ReadOnlyViolation):
        _client_with(conn, handler).get("https://www.fantrax.com/fxea/general/getLeagueInfo?leagueId=x")


def test_redirect_loop_stops(conn):
    import httpx

    from hockey.http import FetchError

    handler = lambda req: httpx.Response(307, headers={"location": "/v1/schedule/now"})  # noqa: E731
    with pytest.raises(FetchError, match="redirects"):
        _client_with(conn, handler).get("https://api-web.nhle.com/v1/schedule/now")
