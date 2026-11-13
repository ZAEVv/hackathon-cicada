"""Witness ledger in network mode: real witness services (uvicorn) on ephemeral loopback ports."""
import contextlib
import copy
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from backend.app import ledger
from backend.app import preclearance as pc
from backend.app.main import create_app
from backend.app.schemas import LedgerStatus
from backend.app.witness_node import create_witness_app

NOW = "2026-11-12T08:00:00Z"
BROKER = {"id": "dev-broker", "role": "broker"}
REVIEWER = {"id": "dev-reviewer", "role": "reviewer"}
SHIPMENT = {"id": "SCT-001", "mass_kg": 20000, "container_id": "C1"}
PAYLOAD = {"shipper": "Exporter", "consignee": "Importer", "description": "Parts",
           "mass_kg": 20000, "value_usd": 100, "origin_country": "RU", "hs_code": "123456",
           "container_id": "C1", "documents": [
               {"kind": kind, "number": kind, "container_id": "C1", "mass_kg": 20000}
               for kind in ("invoice", "packing_list", "transport")]}
NODE_IDS = [node for node, _ in ledger.NODES]


class Server:
    """A real uvicorn server in a background thread on an ephemeral loopback port."""

    def __init__(self, app):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.url = "http://127.0.0.1:%d" % self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "witness server did not start"
            time.sleep(0.01)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()


def dead_url():
    """A loopback URL on which nothing listens (connection refused)."""
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (pc.AUDIT_KEY_ENV, ledger.KEYS_ENV, ledger.URLS_ENV, ledger.TIMEOUT_ENV,
                 "BROKER_TOKEN", "REVIEWER_TOKEN", "DISABLE_DEFAULT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ledger.TIMEOUT_ENV, "1")


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    """Three witness services, each with its own directory and database."""
    servers, paths = {}, {}
    for node in NODE_IDS:
        (tmp_path / node).mkdir()
        paths[node] = str(tmp_path / node / "witness.sqlite3")
        servers[node] = Server(create_witness_app(node, paths[node]))
    urls = {node: servers[node].url for node in NODE_IDS}

    def configure(**override):
        merged = {**urls, **override}
        monkeypatch.setenv(ledger.URLS_ENV, ",".join(f"{n}={merged[n]}" for n in NODE_IDS))

    configure()
    yield {"servers": servers, "paths": paths, "urls": urls, "configure": configure}
    for server in servers.values():
        server.stop()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "main" / "test.sqlite")
    os.makedirs(os.path.dirname(path))
    pc.init_db(path)
    return path


def lifecycle(db):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    pc.submit_declaration(db, SHIPMENT["id"], "key", BROKER, NOW)
    pc.review_declaration(db, SHIPMENT["id"], "needs_revision", "Fix", REVIEWER, NOW)


def states(db):
    status = pc.ledger_status(db, SHIPMENT["id"])
    LedgerStatus(**status)
    return status, {n["node_id"]: n["state"] for n in status["nodes"]}


def node_rows(path):
    with contextlib.closing(__import__("sqlite3").connect(path)) as conn:
        return conn.execute("SELECT sequence, entry_hash FROM attestations ORDER BY sequence").fetchall()


def test_quorum_with_three_network_nodes(db, nodes):
    lifecycle(db)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] and result["ledger"]["status"] == "anchored"
    assert result["ledger"]["nodes_in_sync"] == 3 and result["ledger"]["attested_sequence"] == 4
    status, by_node = states(db)
    assert status["mode"] == "network" and "separate network services" in status["disclaimer"]
    assert by_node == dict.fromkeys(NODE_IDS, "in_sync")
    # Records live only in each node's own database, nothing next to the main database.
    for node in NODE_IDS:
        assert len(node_rows(nodes["paths"][node])) == 4
        assert not os.path.exists(ledger.witness_path(db, node))


def test_one_node_down_still_anchored(db, nodes):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    nodes["servers"]["witness-2"].stop()
    pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] and result["ledger"]["status"] == "anchored"
    assert result["ledger"]["nodes_available"] == 2 and result["ledger"]["attested_sequence"] == 2
    status, by_node = states(db)
    assert by_node == {"witness-1": "in_sync", "witness-2": "unavailable", "witness-3": "in_sync"}
    down = next(n for n in status["nodes"] if n["node_id"] == "witness-2")
    assert down["available"] is False and down["error"] == "unreachable"


def test_two_nodes_down_not_anchored_but_local_write_succeeds(db, nodes):
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    nodes["configure"](**{"witness-1": dead_url(), "witness-3": dead_url()})
    assert pc.verify_audit(db, SHIPMENT["id"])["ledger"]["status"] == "no_quorum"
    assert ledger.anchor(db, SHIPMENT["id"], pc.get_audit(db, SHIPMENT["id"])[0])["anchored"] is False
    validated = pc.validate_declaration(db, SHIPMENT, BROKER, NOW)
    assert validated["status"] == "ready" and len(pc.get_audit(db, SHIPMENT["id"])) == 2
    _, by_node = states(db)
    assert by_node == {"witness-1": "unavailable", "witness-2": "in_sync", "witness-3": "unavailable"}
    # Nodes come back: they hold only entry 1, so entry 2 stays reported as not anchored.
    nodes["configure"]()
    result = pc.verify_audit(db, SHIPMENT["id"])
    assert result["valid"] and result["ledger"]["status"] == "not_anchored"
    assert result["ledger"]["reason"] == "unanchored_entries" and result["ledger"]["attested_sequence"] == 1


def test_slow_node_times_out_and_calls_run_in_parallel(db, nodes, monkeypatch):
    monkeypatch.setenv(ledger.TIMEOUT_ENV, "0.5")
    hanging = [socket.socket() for _ in range(2)]  # accept TCP but never answer
    try:
        urls = {}
        for node, sock in zip(("witness-1", "witness-3"), hanging):
            sock.bind(("127.0.0.1", 0))
            sock.listen(8)
            urls[node] = "http://127.0.0.1:%d" % sock.getsockname()[1]
        nodes["configure"](**urls)
        started = time.monotonic()
        pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
        elapsed = time.monotonic() - started
        assert elapsed < 1.4  # two 0.5 s timeouts in parallel, not in sequence
        status, by_node = states(db)
        assert status["status"] == "no_quorum" and by_node["witness-2"] == "in_sync"
        assert {n["error"] for n in status["nodes"] if not n["available"]} == {"timeout"}
    finally:
        for sock in hanging:
            sock.close()


class Mitm:
    """A man-in-the-middle in front of a real node that rewrites reply bodies."""

    def __init__(self, upstream, rewrite):
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def forward(path: str, request: Request):
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower().startswith("x-witness")}
            if body:
                headers["Content-Type"] = "application/json"
            raw_path = request.scope["raw_path"].decode()
            upstream_request = urllib.request.Request(upstream + raw_path, data=body or None, headers=headers,
                                                      method=request.method)
            try:
                response = urllib.request.urlopen(upstream_request, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data, status = response.read(), response.status
                reply_auth = response.headers.get(ledger.HEADER_REPLY, "")
            return Response(rewrite(data), status_code=status, media_type="application/json",
                            headers={ledger.HEADER_REPLY: reply_auth})

        self.server = Server(app)


def test_tampered_reply_in_transit_is_rejected(db, nodes):
    def forge(data):
        body = json.loads(data)
        if "signature" in body:
            body["signature"] = "0" * 64
        for record in body.get("records", []):
            record["entry_hash"] = "f" * 64
        return json.dumps(body).encode()

    mitm = Mitm(nodes["urls"]["witness-1"], forge)
    try:
        nodes["configure"](**{"witness-1": mitm.server.url})
        outcome_before = len(node_rows(nodes["paths"]["witness-1"]))
        pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
        # The real node stored the record, but its (altered) reply was not counted.
        assert len(node_rows(nodes["paths"]["witness-1"])) == outcome_before + 1
        entry = pc.get_audit(db, SHIPMENT["id"])[0]
        nodes["configure"](**{"witness-1": mitm.server.url})
        status, by_node = states(db)
        assert by_node == {"witness-1": "invalid_signature", "witness-2": "in_sync", "witness-3": "in_sync"}
        bad = next(n for n in status["nodes"] if n["node_id"] == "witness-1")
        assert bad["error"] == "bad_reply" and bad["signature_valid"] is False
        assert status["status"] == "anchored"  # the two honest nodes still form a quorum
        assert entry["hash"] != "f" * 64
    finally:
        mitm.server.stop()


def test_lying_node_records_are_reported_as_divergence(db, nodes):
    lifecycle(db)
    # The node operator rewrites its own store (the reply is authentic, the records are not).
    with contextlib.closing(__import__("sqlite3").connect(nodes["paths"]["witness-3"])) as conn:
        with conn:
            conn.execute("UPDATE attestations SET entry_hash = ? WHERE sequence = 2", ("e" * 64,))
    status, by_node = states(db)
    assert by_node["witness-3"] == "invalid_signature" and status["status"] == "anchored"
    # Two lying nodes: no quorum of valid records, the API never trusts them.
    with contextlib.closing(__import__("sqlite3").connect(nodes["paths"]["witness-1"])) as conn:
        with conn:
            conn.execute("DELETE FROM attestations WHERE sequence > 1")
    status, by_node = states(db)
    assert by_node["witness-1"] == "behind" and status["status"] == "not_anchored"


def post(url, path, body, headers=None):
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def signed_headers(key, method, path, body, timestamp=None, nonce="ab" * 16):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    raw = json.dumps(body).encode()
    return {ledger.HEADER_TIMESTAMP: timestamp, ledger.HEADER_NONCE: nonce,
            ledger.HEADER_AUTH: ledger.request_mac(key, method, path, timestamp, nonce, raw)}


def test_unauthenticated_and_forged_writes_are_refused(nodes):
    url, path = nodes["urls"]["witness-1"], nodes["paths"]["witness-1"]
    record = {"shipment_id": "SCT-001", "sequence": 1, "entry_hash": "a" * 64,
              "previous_hash": "0" * 64, "at": NOW}
    key = ledger.node_key("witness-1")
    assert post(url, "/attest", record) == 401  # no credentials at all
    wrong = signed_headers(ledger.node_key("witness-2"), "POST", "/attest", record)
    assert post(url, "/attest", record, wrong) == 401  # another node's key
    stale = signed_headers(key, "POST", "/attest", record, timestamp=int(time.time()) - 3600)
    assert post(url, "/attest", record, stale) == 401  # outside the clock-skew window
    altered = signed_headers(key, "POST", "/attest", record)
    assert post(url, "/attest", {**record, "entry_hash": "b" * 64}, altered) == 401  # body not signed
    assert node_rows(path) == []
    good = signed_headers(key, "POST", "/attest", record, nonce="cd" * 16)
    assert post(url, "/attest", record, good) == 200
    assert post(url, "/attest", record, good) == 401  # replayed nonce
    fork = {**record, "entry_hash": "c" * 64}
    assert post(url, "/attest", fork, signed_headers(key, "POST", "/attest", fork, nonce="ef" * 16)) == 409
    assert node_rows(path) == [(1, "a" * 64)]
    # Reads are authenticated too; health is open and carries no data.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url + "/records/SCT-001", timeout=5)
    assert excinfo.value.code == 401
    with urllib.request.urlopen(url + "/health", timeout=5) as response:
        assert json.loads(response.read()) == {"node_id": "witness-1", "status": "ok"}


def test_key_mismatch_is_reported_as_rejected(db, nodes, monkeypatch):
    monkeypatch.setenv(ledger.KEYS_ENV, "witness-2=a-different-key-held-by-the-api")
    pc.save_declaration(db, SHIPMENT, copy.deepcopy(PAYLOAD), BROKER, NOW)
    status, by_node = states(db)
    assert by_node["witness-2"] == "unavailable" and status["status"] == "anchored"
    assert next(n for n in status["nodes"] if n["node_id"] == "witness-2")["error"] == "rejected"
    assert node_rows(nodes["paths"]["witness-2"]) == []


def test_url_configuration(monkeypatch):
    monkeypatch.setenv(ledger.URLS_ENV, "http://w1:8100, http://w2:8100/ ,https://w3")
    assert ledger._remote_urls() == {"witness-1": "http://w1:8100", "witness-2": "http://w2:8100",
                                     "witness-3": "https://w3"}
    monkeypatch.setenv(ledger.URLS_ENV, "witness-3=http://c,witness-1=http://a,witness-2=http://b")
    assert ledger._remote_urls()["witness-3"] == "http://c"
    for bad in ("http://a,http://b", "ftp://a,http://b,http://c", "witness-1=http://a,witness-2=http://b",
                "http://user:pw@a,http://b,http://c", "witness-1=http://a,witness-1=http://b,witness-2=http://c"):
        monkeypatch.setenv(ledger.URLS_ENV, bad)
        with pytest.raises(RuntimeError) as excinfo:
            ledger._remote_urls()
        assert "pw" not in str(excinfo.value)
    monkeypatch.setenv(ledger.URLS_ENV, "")
    assert ledger._remote_urls() is None and not ledger.network_mode()


def test_api_ledger_endpoint_in_network_mode(tmp_path, nodes):
    with TestClient(create_app(str(tmp_path / "demo.sqlite3"))) as client:
        headers = {"Authorization": "Bearer dev-broker-token"}
        body = client.get("/api/shipments/SCT-001/ledger", headers=headers).json()
        assert body["mode"] == "network" and body["status"] == "anchored" and body["nodes_available"] == 3
        nodes["servers"]["witness-3"].stop()
        body = client.get("/api/shipments/SCT-001/ledger", headers=headers).json()
        assert body["status"] == "anchored" and body["nodes_available"] == 2
        assert [n["state"] for n in body["nodes"]] == ["in_sync", "in_sync", "unavailable"]
        text = json.dumps(body)
        assert "NOT-A-SECRET" not in text and "127.0.0.1" not in text
