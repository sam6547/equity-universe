"""A small, polite HTTP client.

* identifies itself with one fixed, honest User-Agent
* paces requests per host and slows down when a host pushes back
* retries transient failures, never "not found" answers
* stops calling a host that keeps refusing (circuit breaker), so a blocked or
  broken source is reported on the dashboard rather than hammered

Only the standard library is used so that the weekly job has nothing to install.
"""
from __future__ import annotations

import gzip
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from . import config

# Outcome classes used everywhere else in the pipeline.
OK = "ok"                  # HTTP 200
NOT_FOUND = "not_found"    # the server answered, but has nothing for this request
BLOCKED = "blocked"        # 401/403/429: the server is refusing this client
ERROR = "error"            # network failure, timeout or 5xx
UNAVAILABLE = "unavailable"  # circuit breaker open or time budget spent; no request sent

_BLOCK_CODES = (401, 403, 429, 999)
_NOT_FOUND_CODES = (400, 404, 410, 422)


class Response(object):
    __slots__ = ("kind", "status", "body", "error", "url")

    def __init__(self, kind, status=0, body=b"", error="", url=""):
        self.kind = kind
        self.status = status
        self.body = body
        self.error = error
        self.url = url

    @property
    def ok(self):
        return self.kind == OK

    def describe(self):
        """Short, human-readable reason for the dashboard."""
        if self.kind == OK:
            return "ok"
        if self.kind == UNAVAILABLE:
            return self.error or "source unavailable"
        if self.status:
            return "HTTP %d%s" % (self.status, (": " + self.error) if self.error else "")
        return self.error or self.kind


class HostState(object):
    """Pacing, back-off and circuit-breaker state for one upstream host."""

    def __init__(self, name, base_interval, clock, sleep):
        self.name = name
        self.base_interval = base_interval
        self.interval = base_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_time = 0.0
        self._pause_until = 0.0
        self.epoch = 0                 # bumped on every pause so in-flight requests don't double count
        self.strikes = 0               # consecutive pauses without a success
        self.consecutive_errors = 0
        self.open_reason = ""          # non-empty once the breaker is open
        self.stats = {"requests": 0, "ok": 0, "not_found": 0, "blocked": 0, "error": 0, "bytes": 0, "pauses": 0}

    @property
    def is_open(self):
        return bool(self.open_reason)

    def wait_turn(self):
        """Block until this request may start; returns the epoch it starts in."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_time, self._pause_until)
            self._next_time = start + self.interval
            epoch = self.epoch
        delay = start - now
        if delay > 0:
            self._sleep(delay)
        return epoch

    def on_success(self, kind, nbytes):
        with self._lock:
            self.stats["requests"] += 1
            self.stats[kind] += 1
            self.stats["bytes"] += nbytes
            self.strikes = 0
            self.consecutive_errors = 0
            # recover speed gradually after a slowdown
            self.interval = max(self.base_interval, self.interval * 0.97)

    def on_blocked(self, epoch, retry_after=None):
        """Host refused the request. Pause everyone, slow down, maybe open the breaker."""
        with self._lock:
            self.stats["requests"] += 1
            self.stats["blocked"] += 1
            if epoch != self.epoch:
                return  # another request already triggered the pause for this episode
            self.epoch += 1
            self.strikes += 1
            self.stats["pauses"] += 1
            pause = min(config.BLOCK_PAUSE_BASE_S * (2 ** (self.strikes - 1)), config.BLOCK_PAUSE_MAX_S)
            if retry_after:
                pause = min(max(pause, retry_after), config.BLOCK_PAUSE_MAX_S)
            self._pause_until = self._clock() + pause
            self.interval = min(self.interval * 2, 5.0)
            if self.strikes >= config.BLOCK_MAX_STRIKES:
                self.open_reason = "%s kept refusing requests (rate limit or block) after %d pauses" % (self.name, self.strikes)

    def on_error(self):
        with self._lock:
            self.stats["requests"] += 1
            self.stats["error"] += 1
            self.consecutive_errors += 1
            if self.consecutive_errors >= config.ERROR_MAX_CONSECUTIVE:
                self.open_reason = "%s failed %d times in a row (network/server errors)" % (self.name, self.consecutive_errors)


def _default_transport(url, headers, timeout):
    """Returns (status, body_bytes, response_headers). Raises on network failure."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            rh = resp.headers
    except urllib.error.HTTPError as e:
        body = e.read() or b""
        status = e.code
        rh = e.headers
    if rh is not None and (rh.get("Content-Encoding") or "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            pass
    return status, body, rh


class Client(object):
    def __init__(self, user_agent=None, transport=None, clock=None, sleep=None, deadline=None, intervals=None):
        # type: (Optional[str], Optional[Callable], Optional[Callable], Optional[Callable], Optional[float], Optional[Dict[str, float]]) -> None
        self.user_agent = user_agent or config.USER_AGENT
        self._transport = transport or _default_transport
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self.deadline = deadline          # monotonic time after which no new requests start
        self._intervals = dict(config.HOST_INTERVALS)
        if intervals:
            self._intervals.update(intervals)
        self._hosts = {}                  # type: Dict[str, HostState]
        self._hosts_lock = threading.Lock()

    # -- host bookkeeping -------------------------------------------------
    def host(self, name):
        with self._hosts_lock:
            st = self._hosts.get(name)
            if st is None:
                st = HostState(name, self._intervals.get(name, self._intervals["DEFAULT"]), self._clock, self._sleep)
                self._hosts[name] = st
            return st

    def host_for_url(self, url):
        hostname = urllib.parse.urlsplit(url).hostname or "unknown"
        return self.host(config.HOST_BUCKETS.get(hostname, hostname))

    def stats(self):
        with self._hosts_lock:
            return {name: dict(h.stats, open=h.open_reason) for name, h in self._hosts.items()}

    def out_of_time(self):
        return self.deadline is not None and self._clock() >= self.deadline

    # -- requests ---------------------------------------------------------
    def get(self, url, accept="application/json, text/csv, */*", block_retries=2, error_retries=2):
        # type: (str, str, int, int) -> Response
        host = self.host_for_url(url)
        headers = {"User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": "gzip"}
        blocked_tries = 0
        error_tries = 0
        while True:
            if host.is_open:
                return Response(UNAVAILABLE, error=host.open_reason, url=url)
            if self.out_of_time():
                return Response(UNAVAILABLE, error="time budget for this run was used up", url=url)
            epoch = host.wait_turn()
            if host.is_open:  # may have opened while we waited
                return Response(UNAVAILABLE, error=host.open_reason, url=url)
            try:
                status, body, rh = self._transport(url, headers, config.HTTP_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - any network failure is handled the same way
                host.on_error()
                error_tries += 1
                if error_tries > error_retries:
                    return Response(ERROR, error="%s: %s" % (type(exc).__name__, str(exc)[:120]), url=url)
                self._sleep(min(1.5 * (3 ** (error_tries - 1)), 20) + random.random())
                continue

            if status == 200:
                host.on_success("ok", len(body))
                return Response(OK, status, body, url=url)
            if status in _NOT_FOUND_CODES:
                host.on_success("not_found", len(body))
                return Response(NOT_FOUND, status, body, url=url)
            if status in _BLOCK_CODES:
                host.on_blocked(epoch, _retry_after(rh))
                blocked_tries += 1
                if blocked_tries > block_retries:
                    return Response(BLOCKED, status, body, error="request refused (rate limit or block)", url=url)
                continue  # wait_turn() honours the host-wide pause
            # 5xx and anything unexpected
            host.on_error()
            error_tries += 1
            if error_tries > error_retries:
                return Response(ERROR, status, body, error="server error", url=url)
            self._sleep(min(1.5 * (3 ** (error_tries - 1)), 20) + random.random())


def _retry_after(headers):
    # type: (object) -> Optional[float]
    try:
        value = headers.get("Retry-After") if headers is not None else None
        return float(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def summarize_failure(resp):
    # type: (Response) -> Tuple[str, str]
    """(category, message) for health reporting."""
    return resp.kind, resp.describe()
