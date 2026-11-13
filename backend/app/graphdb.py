"""Corridor topology backends: Neo4j (Cypher) with an in-memory networkx fallback.

The graph answers one question for routing: which chains of scheduled services
lead from hub A to hub B without visiting a hub twice. Timing feasibility
(cutoffs, departures, capacity, cargo) stays in ``routing._schedule``.

Data model in Neo4j::

    (:Hub {id, name, country, lat, lon, kind})
        -[:SERVICE {id, mode, distance_km, duration_hours, departures,
                    cutoff_hours, handling_hours, capacity_tonnes,
                    cost_usd, allowed_cargo}]->
    (:Hub)

A service is a relationship rather than a node: every service has exactly one
origin and one destination, its timetable is a flat list of ISO timestamps (a
valid list property), and parallel services between the same hubs simply
become parallel relationships. A path of SERVICE relationships *is* a service
chain, so no product over per-hop alternatives is needed.

Both backends return the same set: every chain of 1..MAX_HOPS services whose
hub sequence is a simple path. Neo4j additionally drops blocked services
inside the query; the networkx backend leaves that to ``_schedule``, which
rejects any chain containing a blocked service, so the routes are identical.
"""
from __future__ import annotations

import logging
import os
import time
from itertools import product
from typing import Iterable, Protocol

import networkx as nx

logger = logging.getLogger("sct.graph")

# Longest service chain considered. The catalog has 8 hubs (a simple path has
# at most 7 hops), so the cap never cuts a real route; it bounds the Cypher
# variable-length expansion if the network grows.
MAX_HOPS = 8

SERVICE_PROPERTIES = ("mode", "distance_km", "duration_hours", "departures", "cutoff_hours",
                      "handling_hours", "capacity_tonnes", "cost_usd", "allowed_cargo")
HUB_PROPERTIES = ("name", "country", "lat", "lon", "kind")

# Variable-length path over SERVICE relationships. Cypher already forbids
# reusing a relationship within a path; the second predicate additionally
# forbids revisiting a hub (simple paths, as nx.all_simple_paths).
CHAINS_QUERY = f"""
MATCH p = (o:Hub {{id: $origin}})-[:SERVICE*1..{MAX_HOPS}]->(d:Hub {{id: $destination}})
WHERE none(r IN relationships(p) WHERE r.id IN $blocked)
  AND all(i IN range(0, size(nodes(p)) - 2) WHERE NOT nodes(p)[i] IN nodes(p)[i + 1..])
RETURN [r IN relationships(p) | r.id] AS service_ids
"""

UPSERT_QUERIES = (
    "CREATE CONSTRAINT hub_id IF NOT EXISTS FOR (h:Hub) REQUIRE h.id IS UNIQUE",
    "CREATE INDEX service_id IF NOT EXISTS FOR ()-[r:SERVICE]-() ON (r.id)",
)
UPSERT_HUBS = """
UNWIND $hubs AS hub
MERGE (h:Hub {id: hub.id})
SET h += hub.props
WITH collect(hub.id) AS ids
MATCH (stale:Hub) WHERE NOT stale.id IN ids
DETACH DELETE stale
"""
# Drop services that left the catalog or changed endpoints, then merge the rest.
DELETE_STALE_SERVICES = """
MATCH (o:Hub)-[r:SERVICE]->(d:Hub)
WHERE NOT [r.id, o.id, d.id] IN $keys
DELETE r
"""
UPSERT_SERVICES = """
UNWIND $services AS s
MATCH (o:Hub {id: s.origin_id}), (d:Hub {id: s.destination_id})
MERGE (o)-[r:SERVICE {id: s.id}]->(d)
SET r += s.props
"""


class Topology(Protocol):
    name: str

    def service_chains(self, origin: str, destination: str, ports: list[dict],
                       services: list[dict], blocked: Iterable[str]) -> list[tuple[str, ...]]:
        ...


class NetworkxTopology:
    """In-memory topology, rebuilt from the given catalog on every call."""

    name = "networkx"

    def service_chains(self, origin, destination, ports, services, blocked=()):
        graph = nx.DiGraph()
        graph.add_nodes_from(port["id"] for port in ports)
        for service in services:
            graph.add_edge(service["origin_id"], service["destination_id"])
        if origin == destination or origin not in graph or destination not in graph \
                or not nx.has_path(graph, origin, destination):
            return []
        chains = []
        for nodes in nx.all_simple_paths(graph, origin, destination, cutoff=MAX_HOPS):
            groups = [[s["id"] for s in services
                       if s["origin_id"] == a and s["destination_id"] == b]
                      for a, b in zip(nodes, nodes[1:])]
            chains.extend(product(*groups))
        return chains


class Neo4jTopology:
    """Topology stored in Neo4j and enumerated with Cypher."""

    def __init__(self, driver, database: str | None = None):
        self.driver = driver
        self.database = database
        self._fallback = NetworkxTopology()
        self._failed = False

    @property
    def name(self) -> str:
        # What answered the most recent query: "networkx" while Neo4j is failing.
        return "networkx" if self._failed else "neo4j"

    def sync(self, ports: list[dict], services: list[dict]) -> None:
        """Idempotently upsert the catalog (safe to run on every start)."""
        hubs = [{"id": p["id"], "props": {k: p[k] for k in HUB_PROPERTIES if k in p}}
                for p in ports]
        rows = [{"id": s["id"], "origin_id": s["origin_id"], "destination_id": s["destination_id"],
                 "props": {k: s[k] for k in SERVICE_PROPERTIES if k in s}} for s in services]
        keys = [[s["id"], s["origin_id"], s["destination_id"]] for s in services]
        for query in UPSERT_QUERIES:
            self.driver.execute_query(query, database_=self.database)

        def write(tx):
            tx.run(UPSERT_HUBS, hubs=hubs).consume()
            tx.run(DELETE_STALE_SERVICES, keys=keys).consume()
            tx.run(UPSERT_SERVICES, services=rows).consume()

        with self.driver.session(database=self.database) as session:
            session.execute_write(write)

    def query_chains(self, origin: str, destination: str, blocked: Iterable[str] = ()):
        if origin == destination:
            return []
        records, _, _ = self.driver.execute_query(
            CHAINS_QUERY, origin=origin, destination=destination, blocked=sorted(set(blocked)),
            database_=self.database, routing_="r")
        return [tuple(record["service_ids"]) for record in records]

    def service_chains(self, origin, destination, ports, services, blocked=()):
        try:
            chains = self.query_chains(origin, destination, blocked)
        except Exception as exc:  # the DB went away mid-run: answer, do not 500
            if not self._failed:
                logger.warning("Neo4j path query failed (%s); using networkx for this request",
                               type(exc).__name__)
                self._failed = True
            return self._fallback.service_chains(origin, destination, ports, services, blocked)
        if self._failed:
            logger.warning("Neo4j path queries answered again; graph backend: neo4j")
            self._failed = False
        known = {s["id"] for s in services}
        # The DB mirrors the catalog loaded at startup; ignore anything the caller lacks.
        return [chain for chain in chains if all(sid in known for sid in chain)]

    def close(self) -> None:
        self.driver.close()


def connect(ports: list[dict], services: list[dict], *, url: str | None = None,
            attempts: int = 5, delay_seconds: float = 2.0) -> Topology:
    """Neo4j topology if GRAPH_DB_URL is set and reachable, else networkx."""
    url = (url if url is not None else os.getenv("GRAPH_DB_URL") or "").strip()
    if not url:
        logger.info("GRAPH_DB_URL not set; graph backend: networkx (in-memory)")
        return NetworkxTopology()
    try:
        from neo4j import GraphDatabase
    except ImportError:
        logger.warning("neo4j driver not installed; graph backend: networkx (in-memory)")
        return NetworkxTopology()
    user = os.getenv("GRAPH_DB_USER") or "neo4j"
    password = os.getenv("GRAPH_DB_PASSWORD") or ""
    database = os.getenv("GRAPH_DB_DATABASE") or None
    last_error = None
    for attempt in range(max(1, attempts)):
        driver = None
        try:
            driver = GraphDatabase.driver(url, auth=(user, password), connection_timeout=5,
                                          connection_acquisition_timeout=10,
                                          max_transaction_retry_time=2)
            driver.verify_connectivity()
            topology = Neo4jTopology(driver, database)
            topology.sync(ports, services)
            logger.info("Graph backend: neo4j (%d hubs, %d services upserted)",
                        len(ports), len(services))
            return topology
        except Exception as exc:  # unreachable, auth failure, not ready yet
            last_error = exc
            if driver is not None:
                driver.close()
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
    # Never log the URL's credentials or the password; the exception type is enough.
    logger.warning("Neo4j unreachable at startup (%s); graph backend: networkx (in-memory)",
                   type(last_error).__name__)
    return NetworkxTopology()
