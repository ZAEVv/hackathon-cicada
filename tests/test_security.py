"""Tests for backend.app.security (body limit, rate limiting, headers, Host allowlist).

Throwaway FastAPI apps are built here on purpose; backend.app.main is not imported.
"""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import security

ENV_VARS = ("MAX_BODY_BYTES", "RATE_LIMIT_PER_MINUTE", "AUTH_FAIL_LIMIT",
            "AUTH_FAIL_WINDOW_SECONDS", "ALLOWED_HOSTS", "ENABLE_API_DOCS")

EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": security.STRICT_CSP,
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "geolocation=(), camera=(), microphone=()",
    "cache-control": "no-store",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_app(clock=None, lifespan=None, **kwargs):
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    @app.put("/api/json")
    def put_json(payload: dict):
        return {"keys": len(payload)}

    @app.get("/api/private")
    def private(authorization: str | None = Header(default=None)):
        if authorization != "Bearer good":
            raise HTTPException(401, "Valid bearer token required")
        return {"ok": True}

    @app.get("/api/custom-headers")
    def custom_headers():
        return JSONResponse({"ok": True}, headers={"Cache-Control": "max-age=60", "X-Frame-Options": "SAMEORIGIN"})

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("boom")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    security.install(app, clock=clock or FakeClock(), **kwargs)
    return app


def run_asgi(app, method="POST", path="/api/echo", headers=(), chunks=(b"",),
             client=("10.0.0.1", 1234), host=b"testserver"):
    """Drive the ASGI app directly; returns (sent messages, number of body chunks consumed)."""
    async def main():
        raw_headers = ([(b"host", host)] if host is not None else []) + list(headers)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "root_path": "", "query_string": b"", "headers": raw_headers,
                 "client": client, "server": ("testserver", 80)}
        pending = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                   for index, chunk in enumerate(chunks)]
        consumed = 0
        sent = []

        async def receive():
            nonlocal consumed
            if pending:
                consumed += 1
                return pending.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        return sent, consumed

    return asyncio.run(main())


def starts(sent):
    return [m for m in sent if m["type"] == "http.response.start"]


def decoded_headers(start):
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def body_json(sent):
    return json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


def assert_security_headers(headers):
    for name, value in EXPECTED_HEADERS.items():
        assert headers.get(name) == value, name
    assert "strict-transport-security" not in {k.lower() for k in headers}


# --------------------------------------------------------------------------- body limit

def test_content_length_over_limit_is_rejected_before_reading():
    sent, consumed = run_asgi(make_app(), headers=[(b"content-length", b"65537")], chunks=(b"x" * 65537,))
    assert consumed == 0
    (start,) = starts(sent)
    assert start["status"] == 413
    assert body_json(sent) == {"detail": "Request body too large"}
    assert_security_headers(decoded_headers(start))


def test_body_exactly_at_limit_passes_and_one_more_byte_fails(monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")
    client = TestClient(make_app())
    ok = client.post("/api/echo", content=b"x" * 1024)
    assert ok.status_code == 200 and ok.json() == {"size": 1024}
    too_big = client.post("/api/echo", content=b"x" * 1025)
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": "Request body too large"}


def test_chunked_body_without_content_length_is_rejected_via_testclient(monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")

    def stream():
        for _ in range(8):
            yield b"y" * 512

    response = TestClient(make_app()).post("/api/echo", content=stream())
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert_security_headers(response.headers)


@pytest.mark.parametrize("declared", [None, b"10"])
@pytest.mark.parametrize("path, method", [("/api/echo", "POST"), ("/api/json", "PUT")])
def test_streamed_body_overflow_stops_reading_and_sends_exactly_one_413(declared, path, method):
    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", declared))  # lying: the real body is far bigger
    chunks = tuple(b"z" * 16384 for _ in range(100))
    sent, consumed = run_asgi(make_app(), method=method, path=path, headers=headers, chunks=chunks)
    assert consumed == 5  # 5 * 16384 > 65536: stopped right after crossing the limit
    (start,) = starts(sent)  # downstream's own 400/500 attempt was suppressed
    assert start["status"] == 413
    assert body_json(sent) == {"detail": "Request body too large"}
    assert_security_headers(decoded_headers(start))
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len(bodies) == 1 and not bodies[0].get("more_body", False)


def test_small_streamed_body_passes():
    sent, consumed = run_asgi(make_app(), chunks=(b"a" * 1000, b"b" * 1000, b""))
    assert consumed == 3
    assert starts(sent)[0]["status"] == 200
    assert body_json(sent) == {"size": 2000}


@pytest.mark.parametrize("values", [
    [b"abc"], [b"-1"], [b"+5"], [b"1e3"], [b""], [b" "], [b"10, 20"], [b"\xb2"], [b"10", b"11"],
])
def test_malformed_content_length_is_400(values):
    headers = [(b"content-length", value) for value in values]
    sent, consumed = run_asgi(make_app(), headers=headers, chunks=(b"0123456789",))
    assert consumed == 0
    (start,) = starts(sent)
    assert start["status"] == 400
    assert body_json(sent) == {"detail": "Invalid Content-Length header"}
    assert_security_headers(decoded_headers(start))


def test_identical_repeated_content_length_is_accepted():
    sent, _ = run_asgi(make_app(), headers=[(b"content-length", b"4, 4")], chunks=(b"abcd",))
    assert starts(sent)[0]["status"] == 200


def test_absurdly_long_content_length_is_413_not_crash():
    sent, _ = run_asgi(make_app(), headers=[(b"content-length", b"9" * 5000)])
    assert starts(sent)[0]["status"] == 413


def test_lifespan_and_websocket_pass_through():
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield
        events.append("shutdown")

    with TestClient(make_app(lifespan=lifespan)) as client:
        assert events == ["startup"]
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_text() == "hi"
    assert events == ["startup", "shutdown"]


# --------------------------------------------------------------------------- rate limiting

def test_general_rate_limit_returns_429_and_recovers_with_time(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "5")
    clock = FakeClock()
    client = TestClient(make_app(clock))
    assert all(client.get("/api/ping").status_code == 200 for _ in range(5))
    limited = client.get("/api/ping")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many requests"}
    retry_after = int(limited.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert_security_headers(limited.headers)
    # Denied requests are not counted, so hammering does not extend the block.
    for _ in range(20):
        assert client.get("/api/ping").status_code == 429
    clock.advance(retry_after - 1)
    assert client.get("/api/ping").status_code == 429
    clock.advance(1)
    assert client.get("/api/ping").status_code == 200


def test_rolling_window_frees_capacity_gradually(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "4")
    clock = FakeClock()
    client = TestClient(make_app(clock))
    client.get("/api/ping"), client.get("/api/ping")
    clock.advance(30)
    client.get("/api/ping"), client.get("/api/ping")
    assert client.get("/api/ping").status_code == 429
    clock.advance(30)  # the first two expire, the last two are still inside the window
    assert client.get("/api/ping").status_code == 200
    assert client.get("/api/ping").status_code == 200
    response = client.get("/api/ping")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) == 30


def test_auth_failure_lockout_and_time_based_recovery(monkeypatch):
    monkeypatch.setenv("AUTH_FAIL_LIMIT", "3")
    monkeypatch.setenv("AUTH_FAIL_WINDOW_SECONDS", "60")
    clock = FakeClock()
    client = TestClient(make_app(clock))
    for _ in range(3):
        assert client.get("/api/private", headers={"Authorization": "Bearer guess"}).status_code == 401
    locked = client.get("/api/private", headers={"Authorization": "Bearer good"})
    assert locked.status_code == 429
    retry_after = int(locked.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert_security_headers(locked.headers)
    assert client.get("/api/ping").status_code == 429  # lockout covers the whole API ...
    assert client.get("/api/health").status_code == 200  # ... except the liveness probe
    other = TestClient(make_app(clock))
    assert other.get("/api/ping").status_code == 200  # a separate app is unaffected
    clock.advance(retry_after)
    assert client.get("/api/private", headers={"Authorization": "Bearer good"}).status_code == 200


def test_successful_requests_do_not_reset_failures(monkeypatch):
    monkeypatch.setenv("AUTH_FAIL_LIMIT", "2")
    client = TestClient(make_app())
    guess = {"Authorization": "Bearer guess"}
    assert client.get("/api/private", headers=guess).status_code == 401
    assert client.get("/api/private", headers={"Authorization": "Bearer good"}).status_code == 200
    assert client.get("/api/private", headers=guess).status_code == 401
    assert client.get("/api/ping").status_code == 429


def test_tokenless_401s_never_lock_the_client_out(monkeypatch):
    # A foreign web page shares the user's loopback address but cannot attach an
    # Authorization header, so credential-less 401s must not count as guesses.
    monkeypatch.setenv("AUTH_FAIL_LIMIT", "2")
    client = TestClient(make_app())
    assert [client.get("/api/private").status_code for _ in range(10)] == [401] * 10
    assert client.get("/api/private", headers={"Authorization": "Bearer good"}).status_code == 200


def test_cross_site_browser_requests_are_refused_and_not_rate_counted(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    client = TestClient(make_app(allowed_origins=["http://localhost:5173"]))
    foreign = {"Sec-Fetch-Site": "cross-site"}
    for headers in (foreign, {**foreign, "Origin": "https://evil.example"}, {**foreign, "Origin": "null"}):
        for _ in range(5):  # more than the general limit: refusals are free
            refused = client.get("/api/ping", headers=headers)
            assert refused.status_code == 403
            assert refused.json() == {"detail": "Cross-site request refused"}
            assert_security_headers(refused.headers)
    # The app's own CORS origin, same-origin/same-site browsers and non-browser clients pass.
    assert client.get("/api/ping", headers={**foreign, "Origin": "http://LOCALHOST:5173"}).status_code == 200
    assert client.get("/api/ping", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get("/api/ping").status_code == 429  # only the two accepted requests were counted


def test_cross_site_preflight_from_allowed_origin_still_works():
    client = TestClient(make_app(allowed_origins=["http://localhost:5173"]))
    preflight = client.options("/api/ping", headers={
        "Origin": "http://localhost:5173", "Sec-Fetch-Site": "cross-site",
        "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_health_is_still_general_rate_limited(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    client = TestClient(make_app())
    assert [client.get("/api/health").status_code for _ in range(3)] == [200, 200, 429]


def test_limiter_state_is_per_app(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    clock = FakeClock()
    first, second = make_app(clock), make_app(clock)
    assert TestClient(first).get("/api/ping").status_code == 200
    assert TestClient(first).get("/api/ping").status_code == 429
    assert TestClient(second).get("/api/ping").status_code == 200
    assert first.state.security.limiter is not second.state.security.limiter


def test_limits_are_per_client_ip_and_ignore_x_forwarded_for(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app = make_app()
    a = TestClient(app, client=("10.0.0.1", 1))
    b = TestClient(app, client=("10.0.0.2", 1))
    assert a.get("/api/ping").status_code == 200
    assert a.get("/api/ping", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
    assert b.get("/api/ping").status_code == 200


def test_missing_client_uses_shared_unknown_bucket(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app = make_app()
    first, _ = run_asgi(app, method="GET", path="/api/ping", client=None)
    second, _ = run_asgi(app, method="GET", path="/api/ping", client=None)
    assert starts(first)[0]["status"] == 200
    assert starts(second)[0]["status"] == 429
    assert list(app.state.security.limiter.clients) == ["unknown"]


def test_client_table_is_bounded_and_evicts_least_recently_seen():
    app = make_app(max_clients=3)
    for i in range(10):
        run_asgi(app, method="GET", path="/api/ping", client=(f"10.0.0.{i}", 1))
    run_asgi(app, method="GET", path="/api/ping", client=("10.0.0.7", 1))  # refresh 7
    run_asgi(app, method="GET", path="/api/ping", client=("10.0.0.99", 1))
    assert list(app.state.security.limiter.clients) == ["10.0.0.9", "10.0.0.7", "10.0.0.99"]


def test_per_client_memory_is_bounded():
    clock = FakeClock()
    app = make_app(clock)
    limiter = app.state.security.limiter
    for _ in range(500):
        limiter.check("c")
        limiter.record_auth_failure("c")
        clock.advance(0.7)
    state = limiter.clients["c"]
    assert len(state.requests.buckets) <= 30 and len(state.failures.buckets) <= 30


def test_default_limits_tolerate_a_normal_test_suite():
    client = TestClient(make_app())
    statuses = [client.get("/api/private").status_code for _ in range(10)]
    statuses += [client.get("/api/ping").status_code for _ in range(400)]
    assert statuses.count(401) == 10 and statuses.count(200) == 400


# --------------------------------------------------------------------------- headers

def test_security_headers_on_success_404_401_405_and_500():
    client = TestClient(make_app(), raise_server_exceptions=False)
    for path, status in [("/api/ping", 200), ("/missing", 404), ("/api/private", 401), ("/api/boom", 500)]:
        response = client.get(path)
        assert response.status_code == status, path
        assert_security_headers(response.headers)
    response = client.delete("/api/ping")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD, OPTIONS, POST, PUT"
    assert_security_headers(response.headers)


def test_app_set_headers_are_not_overwritten():
    response = TestClient(make_app()).get("/api/custom-headers")
    assert response.headers["cache-control"] == "max-age=60"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.headers.get_list("cache-control")) == 1


@pytest.mark.parametrize("method", ["PATCH", "DELETE", "TRACE", "CONNECT", "PROPFIND"])
def test_unexpected_methods_are_405(method):
    sent, _ = run_asgi(make_app(), method=method, path="/api/ping")
    assert starts(sent)[0]["status"] == 405


def test_cors_preflight_still_works_and_gets_headers():
    response = TestClient(make_app()).options("/api/json", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "Content-Type"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert_security_headers(response.headers)


def test_docs_get_looser_csp_only_when_enabled(monkeypatch):
    strict = TestClient(make_app())
    assert strict.get("/docs").headers["content-security-policy"] == security.STRICT_CSP
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    client = TestClient(make_app())
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == security.DOCS_CSP
        assert response.headers["x-frame-options"] == "DENY"
    assert client.get("/openapi.json").headers["content-security-policy"] == security.STRICT_CSP
    assert client.get("/api/ping").headers["content-security-policy"] == security.STRICT_CSP


# --------------------------------------------------------------------------- host allowlist

@pytest.mark.parametrize("host, status", [
    (b"localhost", 200), (b"localhost:5173", 200), (b"127.0.0.1", 200), (b"127.0.0.1:8000", 200),
    (b"[::1]", 200), (b"[::1]:8000", 200), (b"[0:0:0:0:0:0:0:1]:8000", 200), (b"testserver", 200),
    (b"LOCALHOST:5173", 200), (b"localhost.", 200), (b"localhost.:5173", 200), (b"localhost:", 200),
    (b"evil.example", 400), (b"evil.example:8000", 400), (b"localhost.evil.example", 400),
    (b"127.0.0.2", 400), (b"[::2]", 400), (b"::1", 400), (b"[::1", 400), (b"[::1]x", 400),
    (b"localhost:80:80", 400), (b"localhost:abc", 400), (b"", 400), (b"   ", 400),
    (b"evil.example@localhost", 400), (b"localhost..", 400), (b"local host", 400),
])
def test_host_allowlist_matrix(host, status):
    sent, _ = run_asgi(make_app(), method="GET", path="/api/ping", host=host)
    (start,) = starts(sent)
    assert start["status"] == status
    assert_security_headers(decoded_headers(start))
    if status == 400:
        assert body_json(sent) == {"detail": "Invalid host header"}


def test_missing_and_duplicate_host_rejected():
    sent, _ = run_asgi(make_app(), method="GET", path="/api/ping", host=None)
    assert starts(sent)[0]["status"] == 400
    sent, _ = run_asgi(make_app(), method="GET", path="/api/ping", headers=[(b"host", b"localhost")])
    assert starts(sent)[0]["status"] == 400


def test_bad_host_via_testclient_and_not_counted_by_rate_limiter(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    client = TestClient(make_app())
    for _ in range(5):
        response = client.get("/api/ping", headers={"Host": "rebind.attacker.example"})
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid host header"}
    assert client.get("/api/ping").status_code == 200


def test_websocket_with_bad_host_is_refused():
    client = TestClient(make_app())
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws", headers={"Host": "evil.example"}) as websocket:
            websocket.receive_text()
    assert excinfo.value.code == 1008


def test_allowed_hosts_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", " API.Example.test , *.corp.example, *, ::2, bad host, *.com.,"
                                        " demo.example.org:8443, *bad.example")
    app = make_app()
    settings = app.state.security.settings
    assert "api.example.test" in settings.allowed_hosts and "[::2]" in settings.allowed_hosts
    assert "demo.example.org" in settings.allowed_hosts
    assert settings.allowed_host_suffixes == (".corp.example",)

    def status(host):
        return starts(run_asgi(app, method="GET", path="/api/ping", host=host)[0])[0]["status"]

    assert status(b"api.example.test") == 200
    assert status(b"API.EXAMPLE.TEST.:9000") == 200
    assert status(b"[::2]:8000") == 200
    assert status(b"a.corp.example") == 200
    assert status(b"x.y.corp.example") == 200
    assert status(b"corp.example") == 400
    assert status(b"evilcorp.example") == 400
    assert status(b"anything.example") == 400  # "*" must not enable allow-all
    assert status(b"localhost") == 200  # defaults are kept


# --------------------------------------------------------------------------- config

@pytest.mark.parametrize("value", ["0", "-5", "abc", "", "  ", "1.5", "9" * 40, "١٢"])
def test_garbage_env_falls_back_to_defaults(monkeypatch, value):
    for name in ("MAX_BODY_BYTES", "RATE_LIMIT_PER_MINUTE", "AUTH_FAIL_LIMIT", "AUTH_FAIL_WINDOW_SECONDS"):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENABLE_API_DOCS", "maybe")
    settings = make_app().state.security.settings
    assert settings.max_body_bytes == 65536
    assert settings.rate_limit_per_minute == 600
    assert settings.auth_fail_limit == 20
    assert settings.auth_fail_window_seconds == 300
    assert settings.enable_api_docs is False


def test_valid_env_is_used(monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", " 2048 ")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "100")
    monkeypatch.setenv("AUTH_FAIL_LIMIT", "7")
    monkeypatch.setenv("AUTH_FAIL_WINDOW_SECONDS", "45")
    settings = make_app().state.security.settings
    assert (settings.max_body_bytes, settings.rate_limit_per_minute,
            settings.auth_fail_limit, settings.auth_fail_window_seconds) == (2048, 100, 7, 45)


def test_install_twice_or_after_start_is_refused():
    app = make_app()
    with pytest.raises(RuntimeError):
        security.install(app)
    started = FastAPI()
    TestClient(started).get("/")
    with pytest.raises(RuntimeError):
        security.install(started)
