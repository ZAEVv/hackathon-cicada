"""Stand-alone witness node of the permissioned ledger (network mode, see ``ledger.py``).

Run one process per node::

    WITNESS_NODE_ID=witness-1 WITNESS_DB_PATH=/data/witness.sqlite3 \\
        python -m backend.app.witness_node

The node owns its SQLite file and its signing key (``LEDGER_WITNESS_KEYS``
entry for its id, or the labelled demo key).  Endpoints:

* ``POST /attest`` -- append-only write; 200 with the record signature, 409 when
  the record is not a continuation of this node's own chain for the shipment.
* ``GET /records/{shipment_id}`` and ``GET /head/{shipment_id}``.
* ``GET /health`` -- unauthenticated liveness, no data.

Every request except ``/health`` must carry ``X-Witness-Timestamp``,
``X-Witness-Nonce`` and ``X-Witness-Auth`` (HMAC over method, raw path,
timestamp, nonce and body hash with a key derived from the node key).  Requests
outside the clock-skew window or with a reused nonce are refused with 401.
Every reply carries ``X-Witness-Reply-Auth`` bound to the request nonce.

Environment: ``WITNESS_NODE_ID`` (required), ``WITNESS_DB_PATH`` (default
``./witness-<id>.sqlite3``), ``WITNESS_HOST`` (default 127.0.0.1),
``WITNESS_PORT`` (default 8100), ``LEDGER_WITNESS_KEYS``.
"""
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import ledger

MAX_BODY_BYTES = 2048
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{16,64}$")
_log = logging.getLogger("sct.witness")


class _NonceCache:
    """Single-use nonces within the clock-skew window (bounded memory)."""

    def __init__(self, limit=50_000):
        self._seen, self._lock, self._limit = {}, threading.Lock(), limit

    def use(self, nonce, now):
        with self._lock:
            if len(self._seen) >= self._limit:
                self._seen = {n: t for n, t in self._seen.items() if t > now}
                if len(self._seen) >= self._limit:
                    return False  # flooded: fail closed
            if nonce in self._seen:
                return False
            self._seen[nonce] = now + 2 * ledger.MAX_CLOCK_SKEW
            return True


def _valid_record(data):
    return (isinstance(data, dict) and set(data) == {"shipment_id", "sequence", "entry_hash", "previous_hash", "at"}
            and isinstance(data["shipment_id"], str) and _ID.fullmatch(data["shipment_id"])
            and type(data["sequence"]) is int and 1 <= data["sequence"] <= 1_000_000
            and isinstance(data["entry_hash"], str) and _HASH.fullmatch(data["entry_hash"])
            and isinstance(data["previous_hash"], str) and _HASH.fullmatch(data["previous_hash"])
            and isinstance(data["at"], str) and 0 < len(data["at"]) <= 64 and data["at"].isprintable())


def create_witness_app(node_id, db_path, key=None):
    if node_id not in dict(ledger.NODES):
        raise RuntimeError("Unknown witness node id")
    signing_key = key if key is not None else ledger.node_key(node_id)
    ledger.init_store(db_path)
    nonces = _NonceCache()
    app = FastAPI(title=f"Witness node {node_id}", docs_url=None, redoc_url=None, openapi_url=None)

    def reply(nonce, status, payload):
        body = ledger._json({"node_id": node_id, **payload}).encode("utf-8")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if nonce is not None:
            headers[ledger.HEADER_REPLY] = ledger.reply_mac(signing_key, nonce, status, body)
        return Response(body, status_code=status, media_type="application/json", headers=headers)

    async def authenticate(request):
        """``(nonce, body)`` when the request is authentic, else None. Reads at most MAX_BODY_BYTES."""
        timestamp = request.headers.get(ledger.HEADER_TIMESTAMP, "")
        nonce = request.headers.get(ledger.HEADER_NONCE, "")
        mac = request.headers.get(ledger.HEADER_AUTH, "")
        if not timestamp.isdigit() or len(timestamp) > 12 or not _NONCE.fullmatch(nonce) or not mac:
            return None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                return None
        raw_path = request.scope.get("raw_path") or request.url.path.encode("ascii")
        try:
            path = raw_path.decode("ascii")
        except UnicodeDecodeError:
            return None
        expected = ledger.request_mac(signing_key, request.method, path, timestamp, nonce, bytes(body))
        if not hmac.compare_digest(mac.encode("ascii", "replace"), expected.encode("ascii")):
            return None
        now = time.time()
        if abs(now - int(timestamp)) > ledger.MAX_CLOCK_SKEW or not nonces.use(nonce, now):
            return None
        return nonce, bytes(body)

    @app.get("/health")
    def health():
        return reply(None, 200, {"status": "ok"})

    @app.post("/attest")
    async def attest(request: Request):
        auth = await authenticate(request)
        if auth is None:
            return reply(None, 401, {"detail": "unauthenticated"})
        nonce, body = auth
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if not _valid_record(data):
            return reply(nonce, 422, {"detail": "invalid record"})
        try:
            signature = ledger.append_record(db_path, node_id, signing_key, data["shipment_id"], data["sequence"],
                                             data["entry_hash"], data["previous_hash"], data["at"])
        except (sqlite3.Error, OSError):
            _log.warning("Witness store write failed")
            return reply(nonce, 503, {"detail": "store unavailable"})
        if signature is None:
            return reply(nonce, 409, {"detail": "refused: not an append-only continuation"})
        return reply(nonce, 200, {"signature": signature, "sequence": data["sequence"]})

    async def read(request, shipment_id):
        auth = await authenticate(request)
        if auth is None:
            return None, reply(None, 401, {"detail": "unauthenticated"})
        if not _ID.fullmatch(shipment_id):
            return None, reply(auth[0], 422, {"detail": "invalid shipment id"})
        try:
            rows = ledger.read_records(db_path, shipment_id) or []
        except sqlite3.Error:
            return None, reply(auth[0], 503, {"detail": "store unavailable"})
        return (auth[0], rows), None

    @app.get("/records/{shipment_id}")
    async def records(shipment_id: str, request: Request):
        result, error = await read(request, shipment_id)
        if error is not None:
            return error
        nonce, rows = result
        return reply(nonce, 200, {"shipment_id": shipment_id, "records": [
            dict(zip(("sequence", "entry_hash", "previous_hash", "at", "signature"), row)) for row in rows]})

    @app.get("/head/{shipment_id}")
    async def head(shipment_id: str, request: Request):
        result, error = await read(request, shipment_id)
        if error is not None:
            return error
        nonce, rows = result
        last = rows[-1] if rows else None
        return reply(nonce, 200, {"shipment_id": shipment_id, "last_sequence": last[0] if last else 0,
                                  "head_hash": last[1] if last else ledger.GENESIS_HASH})

    return app


def main():
    import uvicorn

    node_id = os.environ.get("WITNESS_NODE_ID", "")
    if node_id not in dict(ledger.NODES):
        raise SystemExit("WITNESS_NODE_ID must be one of: " + ", ".join(n for n, _ in ledger.NODES))
    db_path = os.environ.get("WITNESS_DB_PATH") or f"./witness-{node_id}.sqlite3"
    host = os.environ.get("WITNESS_HOST", "127.0.0.1")
    port = int(os.environ.get("WITNESS_PORT", "8100"))
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_witness_app(node_id, db_path), host=host, port=port, server_header=False,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
