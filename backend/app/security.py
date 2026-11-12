"""HTTP-level hardening for the local synthetic demo API.

``install(app)`` adds, as pure ASGI layers (no response buffering):

* security response headers on every HTTP response,
* a Host allowlist (DNS-rebinding defence),
* per-client rate limiting plus an auth-failure (HTTP 401) lockout,
* a request-method allowlist,
* a request-body size limit (declared Content-Length and streamed bytes).

Layer order, outermost first, as installed::

    SecurityHeaders      adds headers to *every* response, incl. all below and 500s
    HostCheck            400 for a missing/malformed/non-allowlisted Host
    CrossSite            403 for browser requests initiated by a foreign site
    RateLimit            429 (+ Retry-After); counts 401s of requests that sent credentials
    MethodFilter         405 for methods outside GET/HEAD/POST/PUT/OPTIONS
    BodyLimit            400 bad Content-Length / 413 too large (declared or streamed)
    -- the app's own stack, built by FastAPI/Starlette: --
    ServerErrorMiddleware -> user middleware (CORSMiddleware) -> ExceptionMiddleware -> router

Why this order:

* Headers outermost so the hardening headers also reach our own 400/405/413/429
  responses and the 500 produced by Starlette's ServerErrorMiddleware.  Starlette
  always places ServerErrorMiddleware *outside* every ``add_middleware`` layer, so
  a middleware added with ``add_middleware`` can never decorate an unhandled-error
  500.  Therefore ``install()`` wraps the whole built stack by overriding
  ``app.build_middleware_stack`` on the instance (the same technique the
  OpenTelemetry ASGI contrib uses).  Our layers keep an ``.app`` attribute, so
  stack introspection (e.g. FastAPI's telemetry ``_legacy_otel``) still works.
* Host check before rate accounting: a DNS-rebinding page runs in the local
  user's browser, i.e. it arrives from the same 127.0.0.1 as the user.  If
  rejected-host requests were counted, the attacker could exhaust the user's
  budget.  Host rejections are cheap and are not counted.
* Rate limiting before the method filter and body limit, so floods of 405/413
  requests are still counted.
* Body limit innermost of our layers but outside the app, so it sees the raw
  ``receive`` stream before anything parses it.

Composition with CORSMiddleware: because ``install()`` wraps the *built* stack,
our layers are always outside CORSMiddleware regardless of whether
``security.install(app)`` is called before or after
``app.add_middleware(CORSMiddleware, ...)``.  The only requirements are: call
``install()`` once per app, before the app serves its first request (lifespan
included).  Consequences: CORS preflights (OPTIONS) also pass Host check, rate
limit and method filter (OPTIONS is allowed, so preflights are never 405'd -- the
preflighted method is carried in a header, not as the request method), and our
own 400/405/413/429 responses carry no ``Access-Control-Allow-*`` headers.  That
is acceptable here: the frontend reaches the API same-origin through the Vite
proxy, and a cross-origin caller just sees an opaque failure.

No ``Strict-Transport-Security`` is sent on purpose: the demo is served over
plain HTTP on localhost, where HSTS is ignored by browsers at best and, on a
shared hostname, could pin HTTPS for unrelated local services.  Add it at the
TLS-terminating proxy if this is ever deployed over HTTPS.

Configuration (environment, read once per ``install()`` call; invalid,
zero or negative numbers fall back to the default -- never "unlimited"):

    MAX_BODY_BYTES            65536
    RATE_LIMIT_PER_MINUTE     600     (per client IP, rolling 60 s)
    AUTH_FAIL_LIMIT           20      (401 responses per client ...)
    AUTH_FAIL_WINDOW_SECONDS  300     (... within this rolling window)
    ALLOWED_HOSTS             ""      (extra comma-separated hostnames; "*.x.y" ok)
    ENABLE_API_DOCS           off     (truthy: looser CSP on the docs pages only)

The client identity is ``scope["client"][0]`` as provided by the ASGI server;
``X-Forwarded-For`` is deliberately ignored (it is client-controlled).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Iterable, Mapping, Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

__all__ = ["install"]

DEFAULT_MAX_BODY_BYTES = 65536
DEFAULT_RATE_LIMIT_PER_MINUTE = 600
DEFAULT_AUTH_FAIL_LIMIT = 20
DEFAULT_AUTH_FAIL_WINDOW_SECONDS = 300
DEFAULT_MAX_CLIENTS = 10_000
GENERAL_WINDOW_SECONDS = 60

DEFAULT_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "testserver"})
ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "OPTIONS"})
# Unauthenticated liveness probe: never subject to the auth-failure lockout
# (it cannot produce a 401 and reveals nothing), but still general-rate-limited.
LOCKOUT_EXEMPT_PATHS = frozenset({"/api/health"})

STRICT_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Only used for the Swagger UI / ReDoc HTML pages, and only when ENABLE_API_DOCS
# is truthy.  FastAPI's default docs pages load JS/CSS from cdn.jsdelivr.net,
# an inline bootstrap <script>, fonts from Google (ReDoc), a favicon from
# fastapi.tiangolo.com, and ReDoc spawns a blob: worker.
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; "
    "font-src https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "worker-src blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

_SECURITY_HEADERS = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("content-security-policy", STRICT_CSP),
    ("cross-origin-resource-policy", "same-origin"),
    ("cross-origin-opener-policy", "same-origin"),
    ("permissions-policy", "geolocation=(), camera=(), microphone=()"),
    ("cache-control", "no-store"),
)

_DIGITS = re.compile(r"[0-9]+")
_PORT = re.compile(r"[0-9]{0,5}")
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_HUGE = 10 ** 18  # stands in for absurdly long Content-Length values


# --------------------------------------------------------------------------- config

def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    # Length cap avoids Python's int-from-str digit limit; such values are nonsense anyway.
    if not raw or len(raw) > 15 or not _DIGITS.fullmatch(raw):
        if raw:
            logger.warning("Ignoring invalid %s=%r; using default %s", name, raw, default)
        return default
    value = int(raw)
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r; using default %s", name, raw, default)
        return default
    return value


def parse_host(value: str) -> Optional[str]:
    """Return the normalised hostname of a Host header value, or None if malformed.

    Lower-cases, strips the port and one trailing dot, and canonicalises bracketed
    IPv6 literals (``[0:0::1]:8000`` -> ``[::1]``).
    """
    value = value.strip()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        inner, rest = value[1:end], value[end + 1:]
        if rest and not (rest[0] == ":" and _PORT.fullmatch(rest[1:])):
            return None
        try:
            address = ipaddress.IPv6Address(inner)
        except ValueError:
            return None
        return "[" + address.compressed + "]"
    host, sep, port = value.partition(":")
    if sep and not _PORT.fullmatch(port):  # also rejects a second ':' (bare IPv6)
        return None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > 253 or not _HOSTNAME.fullmatch(host):
        return None
    return host


def _parse_allowed_hosts(raw: str) -> tuple[frozenset, tuple]:
    exact = set(DEFAULT_ALLOWED_HOSTS)
    suffixes = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("*."):
            base = parse_host(entry[2:])
            if base and not base.startswith("[") and "." in base:
                suffixes.append("." + base)
                continue
        elif "*" not in entry:
            if entry.count(":") >= 2 and not entry.startswith("["):
                entry = "[" + entry + "]"  # bare IPv6 such as ::1
            host = parse_host(entry)
            if host:
                exact.add(host)
                continue
        # "*" alone (allow-all) is deliberately refused: it would disable the
        # DNS-rebinding protection by configuration typo.
        logger.warning("Ignoring invalid ALLOWED_HOSTS entry %r", entry)
    return frozenset(exact), tuple(suffixes)


class _Settings:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.max_body_bytes = _positive_int(env, "MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
        self.rate_limit_per_minute = _positive_int(env, "RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE)
        self.auth_fail_limit = _positive_int(env, "AUTH_FAIL_LIMIT", DEFAULT_AUTH_FAIL_LIMIT)
        self.auth_fail_window_seconds = _positive_int(
            env, "AUTH_FAIL_WINDOW_SECONDS", DEFAULT_AUTH_FAIL_WINDOW_SECONDS)
        self.allowed_hosts, self.allowed_host_suffixes = _parse_allowed_hosts(env.get("ALLOWED_HOSTS") or "")
        self.enable_api_docs = (env.get("ENABLE_API_DOCS") or "").strip().lower() in _TRUTHY


# --------------------------------------------------------------------------- helpers

def _encode_headers(pairs: Iterable[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in pairs]


async def _send_json(scope: Scope, send: Send, status: int, detail: str,
                     headers: Iterable[tuple[bytes, bytes]] = ()) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    raw_headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode("ascii")), *headers]
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})


def _client_key(scope: Scope) -> str:
    client = scope.get("client")
    if client:
        host = client[0]
        if isinstance(host, str) and host:
            return host
    return "unknown"


# --------------------------------------------------------------------------- headers

class _SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, headers: list, docs_headers: Optional[list],
                 docs_paths: frozenset) -> None:
        self.app = app
        self.headers = headers
        self.docs_headers = docs_headers
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = self.headers
        if self.docs_headers is not None and scope.get("path") in self.docs_paths:
            extra = self.docs_headers

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = list(message.get("headers") or ())
                present = {bytes(name).lower() for name, _ in existing}
                missing = [(name, value) for name, value in extra if name not in present]
                if missing:  # never overwrite a header the app chose itself
                    message = {**message, "headers": existing + missing}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- host

class _HostCheckMiddleware:
    def __init__(self, app: ASGIApp, allowed: frozenset, suffixes: tuple) -> None:
        self.app = app
        self.allowed = allowed
        self.suffixes = suffixes

    def host_allowed(self, scope: Scope) -> bool:
        values = [value for name, value in scope.get("headers") or () if bytes(name).lower() == b"host"]
        if len(values) != 1:  # missing, or duplicated (request-smuggling smell)
            return False
        host = parse_host(bytes(values[0]).decode("latin-1"))
        if host is None:
            return False
        if host in self.allowed:
            return True
        # "*.example.com" matches "a.example.com", "a.b.example.com", not "example.com".
        return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in self.suffixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self.host_allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Closing before accept makes the server reject the handshake (HTTP 403).
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_json(scope, send, 400, "Invalid host header")


# --------------------------------------------------------------------------- cross-site

class _CrossSiteMiddleware:
    """Refuse browser requests that another *site* initiated.

    Browsers label every request with ``Sec-Fetch-Site``.  A page on a foreign site
    can still fire simple requests (``<img>``, ``fetch(..., {mode: "no-cors"})``) at
    http://127.0.0.1 with a perfectly valid Host header; they arrive from the user's
    own loopback address, so without this check such a page could spend the user's
    rate-limit budget.  ``cross-site`` requests are refused with 403 unless their
    ``Origin`` is one of the app's CORS origins (preflights carry ``Origin`` too).
    Non-browser clients send no ``Sec-Fetch-Site`` and are unaffected; refusals are
    not counted by the rate limiter.
    """

    def __init__(self, app: ASGIApp, allowed_origins: frozenset) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            site = origin = None
            for name, value in scope.get("headers") or ():
                lowered = bytes(name).lower()
                if lowered == b"sec-fetch-site":
                    site = bytes(value).strip().lower()
                elif lowered == b"origin":
                    origin = bytes(value).strip().lower()
            if site == b"cross-site" and origin not in self.allowed_origins:
                await _send_json(scope, send, 403, "Cross-site request refused")
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- rate limit

class _SlidingCounter:
    """Event counter over a rolling window, kept as at most ``span`` time buckets.

    Memory per counter is O(span) regardless of traffic; events expire up to one
    bucket width (window/span seconds) early, never late.
    """
    __slots__ = ("buckets", "total")

    def __init__(self) -> None:
        self.buckets: deque = deque()  # (bucket_index, count), oldest first
        self.total = 0

    def prune(self, current: int, span: int) -> None:
        buckets = self.buckets
        while buckets and buckets[0][0] <= current - span:
            self.total -= buckets.popleft()[1]

    def add(self, current: int) -> None:
        buckets = self.buckets
        if buckets and buckets[-1][0] == current:
            buckets[-1] = (current, buckets[-1][1] + 1)
        else:
            buckets.append((current, 1))
        self.total += 1

    def retry_after(self, limit: int, span: int, width: float, now: float) -> int:
        """Whole seconds until ``total`` drops below ``limit`` (at least 1)."""
        excess = self.total - limit + 1
        removed = 0
        for index, count in self.buckets:
            removed += count
            if removed >= excess:
                return max(1, math.ceil((index + span) * width - now))
        return 1


class _Window:
    __slots__ = ("limit", "span", "width")

    def __init__(self, limit: int, seconds: int, max_buckets: int = 30) -> None:
        self.limit = limit
        self.span = min(max_buckets, seconds)
        self.width = seconds / self.span

    def index(self, now: float) -> int:
        return int(now // self.width)


class _ClientState:
    __slots__ = ("requests", "failures")

    def __init__(self) -> None:
        self.requests = _SlidingCounter()
        self.failures = _SlidingCounter()


class RateLimiter:
    """Per-client request and auth-failure accounting for one app instance.

    State is an LRU-ordered dict capped at ``max_clients`` entries; each entry
    holds two counters of at most 30 buckets, so memory is bounded by
    ``max_clients * 60`` small tuples.  Work per request is O(1) amortised
    (bucket pruning is amortised over the buckets created; Retry-After is only
    computed on denial and scans at most 30 buckets).  All mutation happens in
    synchronous sections under a ``threading.Lock`` that is never held across an
    ``await``, so neither interleaving coroutines nor threads can corrupt it.
    """

    def __init__(self, settings: _Settings, clock: Callable[[], float],
                 max_clients: int = DEFAULT_MAX_CLIENTS) -> None:
        if max_clients < 1:
            raise ValueError("max_clients must be positive")
        self.clock = clock
        self.max_clients = max_clients
        self.general = _Window(settings.rate_limit_per_minute, GENERAL_WINDOW_SECONDS)
        self.auth = _Window(settings.auth_fail_limit, settings.auth_fail_window_seconds)
        self.clients: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def _state(self, key: str) -> _ClientState:
        state = self.clients.get(key)
        if state is None:
            while len(self.clients) >= self.max_clients:
                self.clients.popitem(last=False)  # least recently seen client
            state = self.clients[key] = _ClientState()
        else:
            self.clients.move_to_end(key)
        return state

    def check(self, key: str, lockout_exempt: bool = False) -> Optional[int]:
        """Count one request; return None if allowed, else Retry-After seconds."""
        now = self.clock()
        with self._lock:
            state = self._state(key)
            auth, general = self.auth, self.general
            failures = state.failures
            failures.prune(auth.index(now), auth.span)
            if not lockout_exempt and failures.total >= auth.limit:
                return failures.retry_after(auth.limit, auth.span, auth.width, now)
            requests = state.requests
            current = general.index(now)
            requests.prune(current, general.span)
            if requests.total >= general.limit:
                # Denied requests are not counted, so a client recovers as soon
                # as its accepted traffic slides out of the window.
                return requests.retry_after(general.limit, general.span, general.width, now)
            requests.add(current)
            return None

    def record_auth_failure(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            failures = self._state(key).failures
            current = self.auth.index(now)
            failures.prune(current, self.auth.span)
            failures.add(current)


class _RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = _client_key(scope)
        retry_after = self.limiter.check(key, lockout_exempt=scope.get("path") in LOCKOUT_EXEMPT_PATHS)
        if retry_after is not None:
            await _send_json(scope, send, 429, "Too many requests",
                             [(b"retry-after", str(retry_after).encode("ascii"))])
            return

        # Only a 401 for a request that actually presented credentials is a guess.
        # A web page open in the user's browser shares the user's 127.0.0.1 address
        # but cannot attach an Authorization header cross-site (that needs a CORS
        # preflight, which fails for foreign origins), so token-less 401s must not
        # be able to lock the local user out.
        presented = any(bytes(name).lower() == b"authorization" for name, _ in scope.get("headers") or ())

        async def counting_send(message: Message) -> None:
            if presented and message["type"] == "http.response.start" and message.get("status") == 401:
                self.limiter.record_auth_failure(key)
            await send(message)

        await self.app(scope, receive, counting_send)


# --------------------------------------------------------------------------- methods

class _MethodFilterMiddleware:
    _ALLOW = ", ".join(sorted(ALLOWED_METHODS)).encode("ascii")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") not in ALLOWED_METHODS:
            await _send_json(scope, send, 405, "Method not allowed", [(b"allow", self._ALLOW)])
            return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- body size

_INVALID = object()


def _declared_length(scope: Scope):
    """Content-Length as int, None if absent, or _INVALID if malformed/conflicting."""
    values = set()
    for name, value in scope.get("headers") or ():
        if bytes(name).lower() != b"content-length":
            continue
        for part in bytes(value).split(b","):  # RFC 9110 allows "N, N" for identical values
            part = part.strip()
            if not part or not part.isdigit():  # bytes.isdigit is ASCII-only
                return _INVALID
            values.add(int(part) if len(part) <= 18 else _HUGE)
    if not values:
        return None
    if len(values) != 1:
        return _INVALID
    return values.pop()


class _BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        close = [(b"connection", b"close")]
        declared = _declared_length(scope)
        if declared is _INVALID:
            await _send_json(scope, send, 400, "Invalid Content-Length header", close)
            return
        if declared is not None and declared > self.max_bytes:
            await _send_json(scope, send, 413, "Request body too large", close)
            return

        limit = self.max_bytes
        received = 0
        response_started = False  # by anyone, on the real ``send``
        rejected = False          # we sent our own 413; downstream output is discarded
        cut_off = False           # downstream gets no more body, only http.disconnect

        async def limited_receive() -> Message:
            nonlocal received, response_started, rejected, cut_off
            if cut_off:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    cut_off = True
                    if not response_started:
                        # Flags are set before the first await so a concurrent
                        # downstream send is dropped, never interleaved.
                        response_started = rejected = True
                        await _send_json(scope, send, 413, "Request body too large", close)
                    # Downstream sees a client disconnect (Starlette raises
                    # ClientDisconnect; FastAPI turns that into a 400 that the
                    # guarded send drops).  If the downstream had already started
                    # its response we cannot change the status; we only stop
                    # feeding it the oversized body.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise
            # The client already has a complete 413; this error is the downstream
            # reacting to the truncated body stream.
            logger.debug("Suppressed downstream error after 413", exc_info=True)


# --------------------------------------------------------------------------- install

class SecurityState:
    """Per-app handle stored on ``app.state.security`` (settings + limiter)."""

    def __init__(self, settings: _Settings, limiter: RateLimiter) -> None:
        self.settings = settings
        self.limiter = limiter


def install(app, *, allowed_origins: Iterable[str] = (),
            clock: Callable[[], float] = time.monotonic,
            max_clients: int = DEFAULT_MAX_CLIENTS) -> None:
    """Install the security layers on a FastAPI/Starlette app (see module docstring).

    Must be called once per app, before it serves its first request.
    ``allowed_origins`` are the app's CORS origins: the only origins whose
    cross-site browser requests are let through.  ``clock`` (seconds, monotonic)
    and ``max_clients`` exist for tests.
    """
    if isinstance(allowed_origins, str):
        raise TypeError("allowed_origins must be an iterable of origins, not a string")
    origins = frozenset(origin.strip().lower().encode("latin-1") for origin in allowed_origins)
    if getattr(app.state, "security", None) is not None:
        raise RuntimeError("security.install() was already called for this app")
    if getattr(app, "middleware_stack", None) is not None:
        raise RuntimeError("security.install() must be called before the app starts")

    settings = _Settings(os.environ)
    limiter = RateLimiter(settings, clock, max_clients)
    app.state.security = SecurityState(settings, limiter)

    headers = _encode_headers(_SECURITY_HEADERS)
    docs_headers = None
    docs_paths: frozenset = frozenset()
    if settings.enable_api_docs:
        docs_headers = [(name, value) for name, value in headers if name != b"content-security-policy"]
        docs_headers.append((b"content-security-policy", DOCS_CSP.encode("ascii")))
        docs_url = getattr(app, "docs_url", None)
        candidates = [docs_url, getattr(app, "redoc_url", None)]
        if docs_url:
            candidates.append(getattr(app, "swagger_ui_oauth2_redirect_url", None))
        docs_paths = frozenset(path for path in candidates if path)

    build_app_stack = app.build_middleware_stack  # bound method of the class

    def build_middleware_stack() -> ASGIApp:
        stack = build_app_stack()
        stack = _BodyLimitMiddleware(stack, settings.max_body_bytes)
        stack = _MethodFilterMiddleware(stack)
        stack = _RateLimitMiddleware(stack, limiter)
        stack = _CrossSiteMiddleware(stack, origins)
        stack = _HostCheckMiddleware(stack, settings.allowed_hosts, settings.allowed_host_suffixes)
        return _SecurityHeadersMiddleware(stack, headers, docs_headers, docs_paths)

    app.build_middleware_stack = build_middleware_stack
