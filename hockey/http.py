"""The only way this project talks to the network.

Guarantees, enforced in code (see tests/test_http_guard.py):
  * GET only. There is no method for POST/PUT/DELETE, so no roster move, claim or trade can be sent.
  * Host allowlist. On fantrax.com only the published, keyless /fxea/ API is allowed; the private
    web-app backend (/fxpa/) is refused, per the Fantrax ToS decision recorded in CLAUDE.md.
  * Every response is cached in SQLite with a per-call TTL; per-host rate limiting and backoff.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

log = logging.getLogger(__name__)

HOUR = 3600.0
DAY = 24 * HOUR
FOREVER = None  # ttl=None: never expires

USER_AGENT = "hockey-assistant/0.1 (personal, read-only, cached; low volume)"

# host -> minimum seconds between requests
RATE_LIMITS = {
    "www.fantrax.com": 1.0,
    "api.nhle.com": 0.5,
    "api-web.nhle.com": 0.5,
    "moneypuck.com": 2.0,
}
ALLOWED_PATH_PREFIXES = {
    "www.fantrax.com": ("/fxea/",),
}


class ReadOnlyViolation(Exception):
    """Raised before any request that could leave the read-only, allowlisted surface."""


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{message} ({status}) for {url}")
        self.url = url
        self.status = status


def check_allowed(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ReadOnlyViolation(f"Only https is allowed: {url}")
    host = parts.hostname or ""
    if host not in RATE_LIMITS:
        raise ReadOnlyViolation(f"Host not on the allowlist: {host}")
    prefixes = ALLOWED_PATH_PREFIXES.get(host)
    if prefixes and not parts.path.startswith(prefixes):
        raise ReadOnlyViolation(
            f"Path {parts.path!r} on {host} is not allowed; only {prefixes} (published read-only API)."
        )


def build_url(base: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return base
    return f"{base}?{urlencode({k: v for k, v in params.items() if v is not None})}"


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class HttpClient:
    conn: sqlite3.Connection
    refresh: bool = False
    timeout: float = 30.0
    max_attempts: int = 4
    _last_request: dict[str, float] = field(default_factory=dict)
    _client: httpx.Client | None = None
    stats: dict[str, int] = field(default_factory=lambda: {"hits": 0, "fetches": 0})

    def get(
        self, url: str, params: dict[str, Any] | None = None, *, ttl: float | None = DAY, cache_if=None
    ) -> Response:
        """GET a URL, via the cache.

        ``cache_if`` is an optional predicate on the Response; when it returns False the body is
        not cached (used for Fantrax errors that come back as HTTP 200).
        """
        full = build_url(url, params)
        check_allowed(full)
        if not self.refresh:
            cached = self._cache_lookup(full)
            if cached is not None:
                self.stats["hits"] += 1
                return cached
        resp = self._fetch(full)
        self.stats["fetches"] += 1
        if resp.status == 200 and (cache_if is None or cache_if(resp)):
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache(url, fetched_at, ttl, status, body) VALUES (?,?,?,?,?)",
                (full, time.time(), ttl, resp.status, resp.text),
            )
            self.conn.commit()
        return resp

    def _cache_lookup(self, url: str) -> Response | None:
        row = self.conn.execute(
            "SELECT fetched_at, ttl, status, body FROM http_cache WHERE url=?", (url,)
        ).fetchone()
        if row is None:
            return None
        if row["ttl"] is not None and time.time() - row["fetched_at"] > row["ttl"]:
            return None
        return Response(url, row["status"], row["body"], from_cache=True)

    def _throttle(self, host: str) -> None:
        gap = RATE_LIMITS.get(host, 1.0)
        wait = self._last_request.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _fetch(self, url: str) -> Response:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False
            )
        host = urlsplit(url).hostname or ""
        delay = 2.0
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self._throttle(host)
            try:
                r = self._client.get(url)
            except httpx.TransportError as e:
                last_error = f"transport error: {e}"
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                elif r.is_redirect:
                    # Never follow redirects blindly: they could leave the allowlist.
                    raise FetchError(
                        url, r.status_code, f"unexpected redirect to {r.headers.get('location')}"
                    )
                else:
                    return Response(url, r.status_code, r.text, from_cache=False)
            if attempt < self.max_attempts:
                log.warning("GET %s failed (%s); retrying in %.0fs", url, last_error, delay)
                time.sleep(delay)
                delay *= 2
        raise FetchError(url, None, f"giving up after {self.max_attempts} attempts: {last_error}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
