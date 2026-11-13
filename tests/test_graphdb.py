"""Graph backends: networkx fallback, startup selection and Neo4j parity.

The Neo4j tests run only when GRAPH_DB_TEST_URL points at a *throwaway* Neo4j 5
instance (they replace its :Hub/:SERVICE data with the catalog), e.g.

    docker run -d --rm --name sct-graph-test -p 127.0.0.1:7687:7687 \\
        -e NEO4J_AUTH=neo4j/parity-test-pw neo4j:5.26.10-community
    GRAPH_DB_TEST_URL=bolt://127.0.0.1:7687 GRAPH_DB_TEST_PASSWORD=parity-test-pw pytest tests/test_graphdb.py
"""
import logging
import os
from itertools import product

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from backend.app import data, graphdb, routing
from backend.app.main import create_app

BROKER = {"Authorization": "Bearer dev-broker-token"}
SCENARIOS = ("none", "storm", "congestion", "closure")
GRAPH_ENV = ("GRAPH_DB_URL", "GRAPH_DB_USER", "GRAPH_DB_PASSWORD", "GRAPH_DB_DATABASE")
TEST_URL = os.getenv("GRAPH_DB_TEST_URL", "").strip()
needs_neo4j = pytest.mark.skipif(not TEST_URL, reason="GRAPH_DB_TEST_URL not set (no Neo4j to test against)")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in GRAPH_ENV + ("BROKER_TOKEN", "REVIEWER_TOKEN", "DISABLE_DEFAULT_TOKENS",
                                "DEMO_BROKER_TOKEN", "DEMO_REVIEWER_TOKEN", "DEMO_DISABLE_DEFAULT_TOKENS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def catalog():
    return data.load_demo_data()


def legacy_chains(ports, services, origin, destination):
    """The pre-graphdb enumeration in routing.compute_routes, verbatim (no hop cap)."""
    graph = nx.DiGraph()
    graph.add_nodes_from(p["id"] for p in ports)
    for s in services:
        graph.add_edge(s["origin_id"], s["destination_id"])
    if origin == destination or not nx.has_path(graph, origin, destination):
        return []
    chains = []
    for nodes in nx.all_simple_paths(graph, origin, destination):
        groups = [[s["id"] for s in services if s["origin_id"] == a and s["destination_id"] == b]
                  for a, b in zip(nodes, nodes[1:])]
        chains.extend(product(*groups))
    return chains


def hub_pairs(catalog):
    ids = [p["id"] for p in catalog["ports"]]
    return [(a, b) for a in ids for b in ids]


def blocked_sets(catalog):
    return [(), ("sea-main",)] + [(s["id"],) for s in catalog["services"]]


def test_networkx_topology_matches_legacy_enumeration(catalog):
    topology = graphdb.NetworkxTopology()
    total = 0
    for origin, destination in hub_pairs(catalog):
        expected = legacy_chains(catalog["ports"], catalog["services"], origin, destination)
        got = topology.service_chains(origin, destination, catalog["ports"], catalog["services"], ())
        assert sorted(got) == sorted(expected)
        total += len(got)
    assert total > 0
    # The hop cap never cuts a catalog route: simple paths have at most len(hubs) - 1 hops.
    assert len(catalog["ports"]) - 1 <= graphdb.MAX_HOPS


def test_connect_without_url_uses_networkx(catalog, caplog):
    with caplog.at_level(logging.INFO, logger="sct.graph"):
        topology = graphdb.connect(catalog["ports"], catalog["services"])
    assert topology.name == "networkx"
    assert "GRAPH_DB_URL not set" in caplog.text


def test_connect_unreachable_falls_back_with_log(catalog, caplog, monkeypatch):
    monkeypatch.setenv("GRAPH_DB_PASSWORD", "secret-graph-password")
    with caplog.at_level(logging.WARNING, logger="sct.graph"):
        topology = graphdb.connect(catalog["ports"], catalog["services"],
                                   url="bolt://127.0.0.1:1", attempts=1)
    assert topology.name == "networkx"
    assert "Neo4j unreachable at startup" in caplog.text
    assert "secret-graph-password" not in caplog.text


def test_query_failure_mid_run_falls_back_to_networkx(catalog, caplog):
    class DeadDriver:
        def execute_query(self, *args, **kwargs):
            raise ConnectionError("gone")

    topology = graphdb.Neo4jTopology(DeadDriver())
    with caplog.at_level(logging.WARNING, logger="sct.graph"):
        chains = topology.service_chains("rail_hub", "tehran", catalog["ports"], catalog["services"], ())
    assert sorted(chains) == sorted(legacy_chains(catalog["ports"], catalog["services"], "rail_hub", "tehran"))
    assert "using networkx" in caplog.text
    assert topology.name == "networkx"  # /api/health reports the degraded backend


def test_health_reports_networkx_backend(tmp_path):
    with TestClient(create_app(str(tmp_path / "g.sqlite3"))) as client:
        assert client.get("/api/health").json()["graph_backend"] == "networkx"


# --- Neo4j parity (skipped without GRAPH_DB_TEST_URL) ---------------------------------

@pytest.fixture(scope="module")
def neo4j_topology(catalog):
    if not TEST_URL:
        pytest.skip("GRAPH_DB_TEST_URL not set")
    neo4j = pytest.importorskip("neo4j")
    driver = neo4j.GraphDatabase.driver(
        TEST_URL, auth=(os.getenv("GRAPH_DB_TEST_USER", "neo4j"), os.getenv("GRAPH_DB_TEST_PASSWORD", "")))
    driver.verify_connectivity()
    topology = graphdb.Neo4jTopology(driver)
    topology.sync(catalog["ports"], catalog["services"])
    yield topology
    driver.close()


@pytest.fixture
def graph_env(monkeypatch):
    monkeypatch.setenv("GRAPH_DB_URL", TEST_URL)
    monkeypatch.setenv("GRAPH_DB_USER", os.getenv("GRAPH_DB_TEST_USER", "neo4j"))
    monkeypatch.setenv("GRAPH_DB_PASSWORD", os.getenv("GRAPH_DB_TEST_PASSWORD", ""))


@needs_neo4j
def test_neo4j_sync_is_idempotent_and_drops_stale_data(neo4j_topology, catalog):
    def counts():
        records, _, _ = neo4j_topology.driver.execute_query(
            "MATCH (h:Hub) OPTIONAL MATCH (h)-[r:SERVICE]->() RETURN count(DISTINCT h) AS hubs, count(r) AS services")
        return records[0]["hubs"], records[0]["services"]

    extra_port = {"id": "ghost", "name": "Ghost", "country": "XX", "lat": 0.0, "lon": 0.0, "kind": "port"}
    moved = dict(catalog["services"][0], destination_id="ghost")
    neo4j_topology.sync(catalog["ports"] + [extra_port], catalog["services"][1:] + [moved])
    assert counts() == (len(catalog["ports"]) + 1, len(catalog["services"]))
    for _ in range(2):
        neo4j_topology.sync(catalog["ports"], catalog["services"])
        assert counts() == (len(catalog["ports"]), len(catalog["services"]))
    records, _, _ = neo4j_topology.driver.execute_query(
        "MATCH (o:Hub)-[r:SERVICE]->(d:Hub) RETURN r.id AS id, o.id AS o, d.id AS d, r.departures AS dep")
    stored = {r["id"]: (r["o"], r["d"], r["dep"]) for r in records}
    assert stored == {s["id"]: (s["origin_id"], s["destination_id"], s["departures"]) for s in catalog["services"]}


@needs_neo4j
def test_neo4j_chains_equal_networkx_for_every_hub_pair_and_blockage(neo4j_topology, catalog):
    nx_topology = graphdb.NetworkxTopology()
    compared = 0
    for blocked in blocked_sets(catalog):
        for origin, destination in hub_pairs(catalog):
            expected = {chain for chain in nx_topology.service_chains(
                origin, destination, catalog["ports"], catalog["services"], blocked)
                if not set(chain) & set(blocked)}
            got = neo4j_topology.query_chains(origin, destination, blocked)
            assert len(got) == len(set(got))  # no duplicate chains
            assert set(got) == expected, (origin, destination, blocked)
            compared += 1
    assert compared == len(blocked_sets(catalog)) * len(catalog["ports"]) ** 2


@needs_neo4j
def test_neo4j_routes_equal_networkx_for_all_shipments_and_scenarios(tmp_path, monkeypatch, graph_env, catalog):
    """End to end through the API: every compute_routes call is run on both backends."""
    original = routing.compute_routes
    calls = []

    def both(*args, topology=None, **kwargs):
        assert topology is not None and isinstance(topology, graphdb.Neo4jTopology)
        via_neo4j = original(*args, topology=topology, **kwargs)
        via_networkx = original(*args, topology=graphdb.NetworkxTopology(), **kwargs)
        assert topology.name == "neo4j"  # the Cypher query answered, no silent fallback
        assert via_neo4j == via_networkx
        calls.append((args[0]["id"], args[4]["kind"], len(via_neo4j["alternatives"])))
        return via_neo4j

    monkeypatch.setattr(routing, "compute_routes", both)
    with TestClient(create_app(str(tmp_path / "parity.sqlite3"))) as client:
        assert client.get("/api/health").json()["graph_backend"] == "neo4j"
        for shipment in catalog["shipments"]:
            for kind in SCENARIOS:
                response = client.post(f"/api/shipments/{shipment['id']}/scenario",
                                       json={"kind": kind}, headers=BROKER)
                assert response.status_code == 200, response.text
            assert client.get("/api/dashboard", headers=BROKER).status_code == 200
    covered = {(sid, kind) for sid, kind, _ in calls}
    assert covered == {(s["id"], kind) for s in catalog["shipments"] for kind in SCENARIOS}
    assert any(n for _, kind, n in calls if kind == "closure")  # closure yields real alternatives
