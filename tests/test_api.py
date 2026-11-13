"""Integration coverage for the public synthetic-demo API contract."""
import json
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app import preclearance, routing
from backend.app.main import create_app


BROKER = {"Authorization": "Bearer dev-broker-token"}
REVIEWER = {"Authorization": "Bearer dev-reviewer-token"}
SHIPMENT = "/api/shipments/SCT-001"
READ_ENDPOINTS = ["/api/dashboard", SHIPMENT, f"{SHIPMENT}/preclearance", f"{SHIPMENT}/audit",
                  f"{SHIPMENT}/audit/verify", "/api/catalog", "/api/model/metrics"]
ENV_VARS = ("BROKER_TOKEN", "REVIEWER_TOKEN", "DISABLE_DEFAULT_TOKENS", "ENABLE_API_DOCS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # The outer environment must not change which tokens the tests exercise.
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(tmp_path):
    # Each test gets its own database and application lifespan/in-memory state.
    with TestClient(create_app(str(tmp_path / "demo.sqlite3"))) as test_client:
        yield test_client


def declaration(mass=20000):
    container = "CSPU0000013"
    return {
        "shipper": "ООО «Волга Экспорт»", "consignee": "Caspian Trade Co.",
        "description": "General machine parts", "mass_kg": mass,
        "value_usd": 45000, "origin_country": "RU", "hs_code": "847990",
        "container_id": container,
        "documents": [
            {"kind": "invoice", "number": "INV-001", "container_id": container, "mass_kg": mass},
            {"kind": "packing_list", "number": "PK-001", "container_id": container, "mass_kg": mass},
            {"kind": "transport", "number": "TR-001", "container_id": container, "mass_kg": mass},
        ],
    }


def assert_unauthorized(response):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Valid bearer token required"}


def test_dashboard_and_unknown_shipment_contract(client):
    response = client.get("/api/dashboard", headers=BROKER)
    assert response.status_code == 200
    dashboard = response.json()
    assert dashboard["synthetic"] is True
    assert dashboard["summary"]["total"] == len(dashboard["shipments"]) == 6
    detail = dashboard["shipments"][0]
    assert {"shipment", "weather", "scenario", "prediction", "current_route", "alternatives",
            "selected_route_id", "preclearance", "simulation_now", "synthetic"} <= detail.keys()
    assert {"id", "mass_kg", "events", "planned_service_ids"} <= detail["shipment"].keys()
    assert {"feasible", "legs", "eta", "co2_kg", "emissions"} <= detail["current_route"].keys()
    assert client.get("/api/shipments/not-a-shipment", headers=BROKER).status_code == 404


def test_auth_route_scenarios_and_selection_refresh_reset(client):
    assert client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}).status_code == 401
    assert client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=REVIEWER).status_code == 403
    normal = client.get(SHIPMENT, headers=BROKER).json()
    assert normal["prediction"]["delay_hours"] < 2
    assert normal["current_route"]["feasible"]
    normal_choice = normal["current_route"]["id"]
    chosen_normal = client.post(f"{SHIPMENT}/route", json={"route_id": normal_choice}, headers=BROKER)
    assert chosen_normal.status_code == 200
    assert client.get(SHIPMENT, headers=BROKER).json()["selected_route_id"] == normal_choice
    infeasible = client.post(f"{SHIPMENT}/route", json={"route_id": "not-a-feasible-route"}, headers=BROKER)
    assert infeasible.status_code == 409
    assert infeasible.json() == {"detail": "Route is no longer feasible; refresh alternatives"}
    storm = client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=BROKER)
    assert storm.status_code == 200
    storm = storm.json()
    assert storm["current_route"]["missed_connection"]
    assert storm["alternatives"]
    choice = next(route for route in storm["alternatives"] if route["feasible"])
    selected = client.post(f"{SHIPMENT}/route", json={"route_id": choice["id"]}, headers=BROKER)
    assert selected.status_code == 200
    assert selected.json()["selected_route_id"] == choice["id"]
    refreshed = client.get(SHIPMENT, headers=REVIEWER).json()
    assert refreshed["selected_route_id"] == choice["id"]
    assert refreshed["current_route"]["id"] == choice["id"]
    assert client.post(f"{SHIPMENT}/scenario/reset", headers=REVIEWER).status_code == 403
    assert client.post(f"{SHIPMENT}/scenario/reset").status_code == 401
    reset = client.post(f"{SHIPMENT}/scenario/reset", headers=BROKER)
    assert reset.status_code == 200
    assert reset.json()["scenario"]["kind"] == "none"
    assert reset.json()["selected_route_id"] is None


def test_closure_excludes_sea_main_alternatives(client):
    response = client.post(f"{SHIPMENT}/scenario", json={"kind": "closure"}, headers=BROKER)
    assert response.status_code == 200
    routes = [response.json()["current_route"], *response.json()["alternatives"]]
    assert all("sea-main" not in [leg["service_id"] for leg in route["legs"]] for route in routes)


def test_preclearance_lifecycle_replay_audit_and_readiness_forecast(client):
    before = client.get(SHIPMENT, headers=BROKER).json()
    missing = client.post(f"{SHIPMENT}/preclearance/validate", headers=BROKER)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not found"}
    assert client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted"}, headers=BROKER).status_code == 403
    bad = declaration()
    bad["documents"][1]["mass_kg"] = 19000
    saved = client.put(f"{SHIPMENT}/preclearance", json=bad, headers=BROKER)
    assert saved.status_code == 200
    assert saved.json()["status"] == "draft"
    delayed = client.get(SHIPMENT, headers=BROKER).json()
    assert delayed["shipment"]["documents_ready_at"] > before["shipment"]["documents_ready_at"]
    invalid = client.post(f"{SHIPMENT}/preclearance/validate", headers=BROKER)
    assert invalid.status_code == 200 and invalid.json()["status"] == "validation_failed"
    assert any(issue["code"] == "mass_mismatch" for issue in invalid.json()["validation"]["issues"])
    assert client.post(f"{SHIPMENT}/preclearance/submit", headers={**BROKER, "Idempotency-Key": "bad"}).status_code == 409
    assert client.post(f"{SHIPMENT}/preclearance/submit", headers=BROKER).status_code == 422

    # An untrusted shipper keeps this lifecycle on the manual-review path (see the auto tests below).
    assert client.put(f"{SHIPMENT}/preclearance", json={**declaration(), "shipper": "Unlisted Trader"}, headers=BROKER).status_code == 200
    ready = client.post(f"{SHIPMENT}/preclearance/validate", headers=BROKER)
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    restored = client.get(SHIPMENT, headers=BROKER).json()
    assert restored["shipment"]["documents_ready_at"] == before["shipment"]["documents_ready_at"]

    import uuid
    key = f"test-{uuid.uuid4()}"
    headers = {**BROKER, "Idempotency-Key": key}
    submitted = client.post(f"{SHIPMENT}/preclearance/submit", headers=headers)
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted"
    assert client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted", "comment": "Checked"}, headers=REVIEWER).json()["status"] == "accepted"
    replay = client.post(f"{SHIPMENT}/preclearance/submit", headers=headers)
    assert replay.status_code == 200 and replay.json() == submitted.json()
    audit = client.get(f"{SHIPMENT}/audit", headers=REVIEWER).json()
    assert any(entry["action"] == "submit" for entry in audit)
    assert any(entry["action"] == "review" for entry in audit)
    verification = client.get(f"{SHIPMENT}/audit/verify", headers=REVIEWER).json()
    assert verification["valid"] and verification["entries_checked"] == len(audit)


def _ready(client, **changes):
    assert client.put(f"{SHIPMENT}/preclearance", json={**declaration(), **changes}, headers=BROKER).status_code == 200
    assert client.post(f"{SHIPMENT}/preclearance/validate", headers=BROKER).json()["status"] == "ready"


def test_trusted_declaration_is_auto_accepted_and_replays(client):
    _ready(client)
    headers = {**BROKER, "Idempotency-Key": "auto-1"}
    first = client.post(f"{SHIPMENT}/preclearance/submit", headers=headers)
    assert first.status_code == 200 and first.json()["status"] == "auto_accepted"
    clearance = first.json()["clearance"]
    assert clearance["decision"] == "auto_accepted" and clearance["ruleset_id"] == "rules-v1"
    assert all(r["passed"] for r in clearance["results"])
    assert clearance["ruleset_hash"] == client.get("/api/preclearance/ruleset", headers=REVIEWER).json()["ruleset_hash"]
    assert client.post(f"{SHIPMENT}/preclearance/submit", headers=headers).json() == first.json()
    review = client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted"}, headers=REVIEWER)
    assert review.status_code == 409
    assert client.get(f"{SHIPMENT}/audit/verify", headers=REVIEWER).json()["valid"]
    last = client.get(f"{SHIPMENT}/audit", headers=REVIEWER).json()[-1]
    assert last["action"] == "submit" and last["payload"]["clearance"]["decision"] == "auto_accepted"


@pytest.mark.parametrize("changes,rule", [({"shipper": "Unlisted Trader"}, "R4"), ({"hs_code": "930100"}, "R2")])
def test_failed_rule_goes_to_manual_review_then_reviewer_decides(client, changes, rule):
    _ready(client, **changes)
    submitted = client.post(f"{SHIPMENT}/preclearance/submit", headers={**BROKER, "Idempotency-Key": "m-1"}).json()
    assert submitted["status"] == "submitted" and submitted["clearance"]["decision"] == "manual_review"
    assert [r["rule_id"] for r in submitted["clearance"]["results"] if not r["passed"]] == [rule]
    reviewed = client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted"}, headers=REVIEWER)
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "accepted"


def test_ruleset_endpoint_requires_auth_and_lists_rules(client):
    assert_unauthorized(client.get("/api/preclearance/ruleset"))
    body = client.get("/api/preclearance/ruleset", headers=BROKER).json()
    assert body["id"] == "rules-v1" and len(body["ruleset_hash"]) == 64
    assert [r["id"] for r in body["rules"]] == ["R1", "R2", "R3", "R4", "R5"]


# --- authentication -------------------------------------------------------

@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_require_a_valid_token_of_either_role(client, path):
    assert_unauthorized(client.get(path))
    assert_unauthorized(client.get(path, headers={"Authorization": "Bearer invalid"}))
    assert_unauthorized(client.get(path, headers={"Authorization": "Basic ZGVtbzpkZW1v"}))
    assert client.get(path, headers=BROKER).status_code == 200
    assert client.get(path, headers=REVIEWER).status_code == 200


def test_every_api_route_except_health_rejects_anonymous_requests(client):
    routes = [r for r in client.app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/")]
    checked = 0
    for route in routes:
        path = route.path.replace("{shipment_id}", "SCT-001")
        for method in route.methods:
            response = client.request(method, path)
            if route.path == "/api/health":
                assert response.status_code == 200
            else:
                assert_unauthorized(response)
                checked += 1
    assert checked >= 13


def test_health_is_public_and_reveals_nothing_new(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "simulation_now": "2026-11-12T08:00:00Z", "demo_only": True,
                               "graph_backend": "networkx"}


def test_unknown_shipment_is_rejected_before_lookup_without_token(client):
    # 401 precedes 404 so anonymous callers cannot enumerate shipment IDs.
    assert_unauthorized(client.get("/api/shipments/not-a-shipment"))


@pytest.mark.parametrize("method,path,body", [
    ("post", f"{SHIPMENT}/scenario", {"kind": "storm"}),
    ("post", f"{SHIPMENT}/scenario/reset", None),
    ("post", f"{SHIPMENT}/route", {"route_id": "x"}),
    ("put", f"{SHIPMENT}/preclearance", declaration()),
    ("post", f"{SHIPMENT}/preclearance/validate", None),
    ("post", f"{SHIPMENT}/preclearance/submit", None),
])
def test_broker_mutations_reject_reviewer(client, method, path, body):
    headers = {**REVIEWER, "Idempotency-Key": "k"}
    response = client.request(method.upper(), path, json=body, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Broker role required"}


def test_review_rejects_broker_with_403(client):
    response = client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted"}, headers=BROKER)
    assert response.status_code == 403


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
def test_bearer_scheme_is_case_insensitive(client, scheme):
    assert client.get(SHIPMENT, headers={"Authorization": f"{scheme} dev-broker-token"}).status_code == 200


@pytest.mark.parametrize("value", [
    "Bearer", "Bearer ", "dev-broker-token", "Bearerdev-broker-token",
    "Bearer dev-broker-token extra", "Token dev-broker-token",
    "Bearer " + "dev-broker-token" + " " * 600,
    "Bearer " + "a" * 600,
])
def test_malformed_or_oversized_authorization_is_401(client, value):
    response = client.get(SHIPMENT, headers={"Authorization": value})
    assert_unauthorized(response)


def test_non_ascii_token_is_401_not_500_and_never_echoed(client):
    raw = "Bearer café-s3cr3t".encode("utf-8")
    response = client.get(SHIPMENT, headers={"Authorization": raw})
    assert_unauthorized(response)
    wrong = client.get(SHIPMENT, headers={"Authorization": "Bearer my-guessed-s3cr3t"})
    assert_unauthorized(wrong)
    assert "s3cr3t" not in wrong.text and "s3cr3t" not in str(wrong.headers)


@pytest.mark.parametrize("value", ["1", "true", "YES", " yes "])
def test_default_tokens_rejected_when_disabled(client, monkeypatch, value):
    monkeypatch.setenv("DISABLE_DEFAULT_TOKENS", value)
    assert_unauthorized(client.get(SHIPMENT, headers=BROKER))
    assert_unauthorized(client.get(SHIPMENT, headers=REVIEWER))
    monkeypatch.setenv("BROKER_TOKEN", "configured-broker")
    assert client.get(SHIPMENT, headers={"Authorization": "Bearer configured-broker"}).status_code == 200


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_default_tokens_kept_when_disable_flag_is_falsy(client, monkeypatch, value):
    monkeypatch.setenv("DISABLE_DEFAULT_TOKENS", value)
    assert client.get(SHIPMENT, headers=BROKER).status_code == 200


@pytest.mark.parametrize("header", [
    {"Forwarded": "for=203.0.113.9"}, {"X-Forwarded-For": "203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
])
def test_default_tokens_rejected_behind_forwarding_proxy(client, monkeypatch, header):
    assert_unauthorized(client.get(SHIPMENT, headers={**BROKER, **header}))
    assert_unauthorized(client.get(SHIPMENT, headers={**REVIEWER, **header}))
    monkeypatch.setenv("REVIEWER_TOKEN", "configured-reviewer")
    configured = {"Authorization": "Bearer configured-reviewer", **header}
    assert client.get(SHIPMENT, headers=configured).status_code == 200


def test_default_tokens_rejected_from_non_loopback_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_TOKEN", "configured-broker")
    app = create_app(str(tmp_path / "remote.sqlite3"))
    with TestClient(app, client=("203.0.113.5", 40000)) as remote:
        assert_unauthorized(remote.get(SHIPMENT, headers=REVIEWER))
        assert remote.get(SHIPMENT, headers={"Authorization": "Bearer configured-broker"}).status_code == 200
    for peer in ("127.0.0.1", "::1"):
        with TestClient(app, client=(peer, 40000)) as local:
            assert local.get(SHIPMENT, headers=REVIEWER).status_code == 200


def test_env_token_overrides_default_per_role(client, monkeypatch):
    # Env is read per request, so setting it after create_app takes effect.
    monkeypatch.setenv("BROKER_TOKEN", "configured-broker")
    assert_unauthorized(client.get(SHIPMENT, headers=BROKER))
    configured = {"Authorization": "Bearer configured-broker"}
    assert client.get(SHIPMENT, headers=configured).status_code == 200
    assert client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=configured).status_code == 200
    # The reviewer role is still on its (loopback-only) default.
    assert client.get(SHIPMENT, headers=REVIEWER).status_code == 200
    assert client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=REVIEWER).status_code == 403


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_env_token_is_treated_as_unset(client, monkeypatch, blank):
    monkeypatch.setenv("BROKER_TOKEN", blank)
    assert_unauthorized(client.get(SHIPMENT, headers={"Authorization": "Bearer "}))
    assert client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=BROKER).status_code == 200


def test_identical_tokens_fail_closed(client, monkeypatch, caplog):
    monkeypatch.setenv("BROKER_TOKEN", "same-token")
    monkeypatch.setenv("REVIEWER_TOKEN", "same-token")
    same = {"Authorization": "Bearer same-token"}
    with caplog.at_level(logging.WARNING, logger="sct"):
        assert_unauthorized(client.get(SHIPMENT, headers=same))
        assert_unauthorized(client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=same))
        assert_unauthorized(client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted"}, headers=same))
    assert "identical" in caplog.text and "same-token" not in caplog.text


def test_env_token_equal_to_other_roles_default_fails_closed(client, monkeypatch):
    monkeypatch.setenv("BROKER_TOKEN", "dev-reviewer-token")
    assert_unauthorized(client.get(SHIPMENT, headers=REVIEWER))
    assert_unauthorized(client.get(SHIPMENT, headers=BROKER))


# --- error handling / disclosure -------------------------------------------

@pytest.mark.parametrize("suffix,method", [
    ("", "GET"), ("/preclearance", "GET"), ("/audit", "GET"), ("/audit/verify", "GET"),
    ("/scenario/reset", "POST"), ("/preclearance/validate", "POST"),
])
def test_unknown_shipment_404_is_generic(client, suffix, method):
    shipment_id = "no-such-shipment-xyz"
    response = client.request(method, f"/api/shipments/{shipment_id}{suffix}", headers=BROKER)
    assert response.status_code == 404
    assert response.json() == {"detail": "Shipment not found"}
    assert shipment_id not in response.text


@pytest.fixture
def lenient_client(tmp_path):
    with TestClient(create_app(str(tmp_path / "err.sqlite3")), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize("error", [ValueError("internal ValueError secret-detail"),
                                   KeyError("internal-KeyError-secret-detail"),
                                   PermissionError("internal PermissionError secret-detail")])
def test_internal_routing_errors_are_generic_500(lenient_client, monkeypatch, caplog, error):
    def boom(*args, **kwargs):
        raise error
    monkeypatch.setattr(routing, "compute_routes", boom)
    with caplog.at_level(logging.ERROR, logger="sct"):
        for response in (lenient_client.get(SHIPMENT, headers=BROKER),
                         lenient_client.get("/api/dashboard", headers=BROKER),
                         lenient_client.post(f"{SHIPMENT}/scenario", json={"kind": "storm"}, headers=BROKER)):
            assert response.status_code == 500
            assert response.json() == {"detail": "Internal server error"}
            assert "secret-detail" not in response.text
    records = [r for r in caplog.records if r.name == "sct"]
    assert records and all(r.levelno == logging.ERROR for r in records)
    assert "dev-broker-token" not in caplog.text


def test_unexpected_preclearance_errors_are_500(lenient_client, monkeypatch):
    def read_bug(*args, **kwargs):
        raise ValueError("read path bug secret-detail")

    def decode_bug(*args, **kwargs):
        json.loads("{not json")

    monkeypatch.setattr(preclearance, "get_audit", read_bug)
    response = lenient_client.get(f"{SHIPMENT}/audit", headers=BROKER)
    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    # JSONDecodeError subclasses ValueError but is not a documented domain error.
    monkeypatch.setattr(preclearance, "save_declaration", decode_bug)
    response = lenient_client.put(f"{SHIPMENT}/preclearance", json=declaration(), headers=BROKER)
    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}


def test_documented_preclearance_errors_keep_their_status(client):
    assert client.post(f"{SHIPMENT}/preclearance/submit", headers={**BROKER, "Idempotency-Key": "k"}).json() == {"detail": "Not found"}
    assert client.put(f"{SHIPMENT}/preclearance", json=declaration(), headers=BROKER).status_code == 200
    not_ready = client.post(f"{SHIPMENT}/preclearance/review", json={"decision": "accepted"}, headers=REVIEWER)
    assert not_ready.status_code == 409
    assert not_ready.json() == {"detail": "Only submitted declarations can be reviewed"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_docs_disabled_by_default(client, path):
    assert client.get(path).status_code == 404


def test_api_docs_can_be_enabled_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    with TestClient(create_app(str(tmp_path / "docs.sqlite3"))) as docs_client:
        assert docs_client.get("/openapi.json").status_code == 200
        assert docs_client.get("/docs").status_code == 200


def test_eastern_branch_shipments_and_amirabad_alternative_under_closure(client):
    dashboard = client.get("/api/dashboard", headers=BROKER).json()
    ids = [row["shipment"]["id"] for row in dashboard["shipments"]]
    assert ids == [f"SCT-00{n}" for n in range(1, 7)]

    closed = client.post(f"{SHIPMENT}/scenario", json={"kind": "closure"}, headers=BROKER).json()
    assert closed["current_route"]["feasible"] is False
    assert any(
        leg["destination_id"] == "amirabad" and leg["service_id"] == "sea-mak-amirabad"
        for alt in closed["alternatives"] for leg in alt["legs"]
    )
    for alt in closed["alternatives"]:
        assert all(leg["service_id"] != "sea-main" for leg in alt["legs"])

    # Eastern shipments resolve routes through Amirabad under the base scenario.
    for shipment_id in ("SCT-004", "SCT-005", "SCT-006"):
        detail = client.get(f"/api/shipments/{shipment_id}", headers=BROKER).json()
        services = [leg["service_id"] for leg in detail["current_route"]["legs"]]
        assert services[-1] == "rail-amirabad"
        assert detail["current_route"]["legs"][-1]["destination_id"] == "tehran"
