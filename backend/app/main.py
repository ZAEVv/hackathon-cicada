"""Local, synthetic case-2 demo API and domain integration."""
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import os
from threading import RLock
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import data, hubs, ledger, preclearance, predictive, routing, security, summary
from .schemas import (
    SIMULATION_NOW, CorridorSummary, AuditEntry, AuditVerification, ChooseRouteRequest,
    Dashboard, Declaration, Ruleset, DeclarationInput, IdempotencyKey, ModelMetrics,
    ReviewRequest, ScenarioRequest, ShipmentDetail, ShipmentId,
)
from .schemas import HubForecast
from .schemas import LedgerStatus

logger = logging.getLogger("sct")

TRUTHY = frozenset({"1", "true", "yes"})
TOKEN_ENV = {"broker": "DEMO_BROKER_TOKEN", "reviewer": "DEMO_REVIEWER_TOKEN"}
DEFAULT_TOKENS = {"broker": "demo-broker-token", "reviewer": "demo-reviewer-token"}
USERS = {"broker": {"id": "demo-broker", "role": "broker"},
         "reviewer": {"id": "demo-reviewer", "role": "reviewer"}}
# Transport peers allowed to use the built-in demo tokens. "testclient" is the
# peer name Starlette's TestClient reports.
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip")
MAX_AUTHORIZATION_LENGTH = 512
AUTH_REQUIRED = "Valid demo bearer token required"


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in TRUTHY


def _digest(value: str) -> bytes:
    # Hashing first gives compare_digest equal-length bytes (no length signal)
    # and makes arbitrary non-ASCII input safe to compare.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


def _configured_tokens() -> dict[str, tuple[str, bool]]:
    """{role: (token, is_builtin_default)}, read from the environment on every call."""
    tokens = {}
    for role, env in TOKEN_ENV.items():
        value = (os.getenv(env) or "").strip()
        tokens[role] = (value, False) if value else (DEFAULT_TOKENS[role], True)
    return tokens


def _defaults_allowed(request: Request) -> bool:
    if _truthy("DEMO_DISABLE_DEFAULT_TOKENS"):
        return False
    if request.client is None or request.client.host not in LOOPBACK_PEERS:
        return False
    # A same-host reverse proxy makes every client look like loopback.
    return not any(name in request.headers for name in FORWARDING_HEADERS)


def _bearer_credentials(authorization: str | None) -> str | None:
    if not authorization or len(authorization) > MAX_AUTHORIZATION_LENGTH:
        return None
    scheme, _, credentials = authorization.strip().partition(" ")
    credentials = credentials.strip()
    if scheme.lower() != "bearer" or not credentials or any(c.isspace() for c in credentials):
        return None
    return credentials


def _unauthorized() -> HTTPException:
    return HTTPException(401, AUTH_REQUIRED, headers={"WWW-Authenticate": "Bearer"})


@contextmanager
def _domain_errors():
    """Map the documented preclearance errors to 403/404/409.

    Only the exact built-in types are mapped; subclasses such as
    json.JSONDecodeError or UnicodeError are internal faults and propagate to the
    generic 500 handler. KeyError never echoes its argument.
    """
    try:
        yield
    except Exception as exc:
        kind = type(exc)
        if kind is PermissionError:
            raise HTTPException(403, str(exc)) from None
        if kind is KeyError:
            raise HTTPException(404, "Not found") from None
        if kind is ValueError:
            raise HTTPException(409, str(exc)) from None
        raise


def create_app(db_path: str | None = None) -> FastAPI:
    lock = RLock()
    identical_tokens_logged = []

    @asynccontextmanager
    async def lifespan(app):
        app.state.db = db_path or os.getenv("DATABASE_PATH", "./smart_caspian_demo.sqlite3")
        app.state.catalog = data.load_demo_data()
        app.state.model, app.state.metrics = predictive.train_model(app.state.catalog["history"])
        app.state.shipments = {s["id"]: s for s in app.state.catalog["shipments"]}
        app.state.scenarios = {}
        app.state.selected = {}
        preclearance.init_db(app.state.db)
        ledger.init_witnesses(app.state.db)  # simulated in-process witness nodes
        yield

    docs = _truthy("ENABLE_API_DOCS")
    app = FastAPI(title="Smart Caspian Transit — synthetic demo", lifespan=lifespan,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)
    origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Body limit, rate limiting, Host allowlist, cross-site refusal, security headers.
    security.install(app, allowed_origins=origins)
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        # Path only: never log headers (tokens), query strings or request bodies.
        logger.error("Unhandled %s on %s %r", type(exc).__name__, request.method, request.url.path,
                     exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo the rejected input: it may hold NaN/Infinity or lone surrogates
        # (which cannot be serialised and would turn the 422 into a 500), bidi
        # controls, or simply a large payload.
        errors = [{key: error[key] for key in ("type", "loc", "msg") if key in error}
                  for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    def actor(request: Request, authorization: str | None = Header(default=None)):
        presented = _bearer_credentials(authorization)
        if presented is None:
            raise _unauthorized()
        tokens = _configured_tokens()
        if tokens["broker"][0] == tokens["reviewer"][0]:
            # Fail closed: one role would otherwise silently shadow the other.
            if not identical_tokens_logged:
                identical_tokens_logged.append(True)
                logger.warning("Broker and reviewer tokens are identical; rejecting all bearer tokens")
            raise _unauthorized()
        defaults_ok = _defaults_allowed(request)
        presented_digest = _digest(presented)
        matched = None
        for role, (token, is_default) in tokens.items():
            # No early exit: every configured token is compared on every request.
            if hmac.compare_digest(presented_digest, _digest(token)) and (defaults_ok or not is_default):
                matched = role
        if matched is None:
            raise _unauthorized()
        return dict(USERS[matched])

    def broker(user=Depends(actor)):
        if user["role"] != "broker":
            raise HTTPException(403, "Broker role required")
        return user

    authenticated = [Depends(actor)]

    def shipment(shipment_id):
        item = app.state.shipments.get(shipment_id)
        if item is None:
            raise HTTPException(404, "Shipment not found")
        return deepcopy(item)

    def detail(shipment_id):
        item = shipment(shipment_id)
        declaration = preclearance.get_declaration(app.state.db, shipment_id)
        if declaration and declaration["status"] not in {"ready", "submitted", "auto_accepted", "demo_accepted"}:
            original = datetime.fromisoformat(item["documents_ready_at"].replace("Z", "+00:00"))
            delayed = datetime.fromisoformat(SIMULATION_NOW.replace("Z", "+00:00")) + timedelta(hours=12)
            item["documents_ready_at"] = max(original, delayed).isoformat().replace("+00:00", "Z")
        profile = routing.scenario_profile(app.state.scenarios.get(shipment_id, "none"))
        weather = data.get_weather(item["current_location_id"])
        # The cutoff comes from the original plan (not the document-delayed copy),
        # i.e. the same departure the route baseline below is measured against.
        cutoff = routing.planned_cutoff(shipment(shipment_id), app.state.catalog["services"], SIMULATION_NOW)
        forecast = predictive.predict(app.state.model, item, weather, profile, SIMULATION_NOW, cutoff=cutoff)
        # Keep the original document-readiness plan for the route baseline. The
        # forecast already carries any document-induced delay; changing both would
        # silently move the planned sailing and hide a missed connection.
        routes = routing.compute_routes(shipment(shipment_id), app.state.catalog["ports"],
                                        app.state.catalog["services"], forecast, profile, SIMULATION_NOW)
        selected_id = app.state.selected.get(shipment_id)
        if selected_id:
            candidate = next((r for r in [routes["current_route"], *routes["alternatives"]]
                              if r["id"] == selected_id and r["feasible"]), None)
            if candidate:
                previous = routes["current_route"]
                routes["current_route"] = candidate
                routes["alternatives"] = [r for r in routes["alternatives"] if r["id"] != selected_id]
                if previous["id"] != selected_id:
                    routes["alternatives"].append(previous)
            else:
                app.state.selected.pop(shipment_id, None)
                selected_id = None
        # Refresh future event estimates with the same scenario/route as the KPI.
        # Planned and observed times remain immutable source values.
        arrival = datetime.fromisoformat(item["planned_port_arrival"].replace("Z", "+00:00"))
        arrival += timedelta(hours=item["observed_delay_hours"] + profile["arrival_delay_hours"])
        for event in item["events"]:
            if event.get("actual_at"):
                continue
            if event["event_type"] == "port_arrival":
                event["estimated_at"] = arrival.isoformat().replace("+00:00", "Z")
            elif event["event_type"] == "sea_departure":
                matching = next((leg for leg in routes["current_route"]["legs"]
                                 if leg["mode"] == "sea" and leg["origin_id"] == event["location_id"]), None)
                event["estimated_at"] = matching["departure_at"] if matching else None
            elif event["event_type"] == "delivery":
                event["estimated_at"] = routes["current_route"]["eta"]
            else:
                continue
            event["source"] = "synthetic-model"
            event["quality_flag"] = "forecast"
        return ShipmentDetail(shipment=item, weather=weather, scenario=profile, prediction=forecast,
                              selected_route_id=selected_id, preclearance=declaration, **routes)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "simulation_now": SIMULATION_NOW, "demo_only": True}

    @app.get("/api/catalog", dependencies=authenticated)
    def catalog():
        return {key: app.state.catalog[key] for key in ("ports", "services")}

    @app.get("/api/model/metrics", response_model=ModelMetrics, dependencies=authenticated)
    def metrics():
        return app.state.metrics

    @app.get("/api/dashboard", response_model=Dashboard, dependencies=authenticated)
    def dashboard():
        with lock:
            details = [detail(sid) for sid in app.state.shipments]
            return Dashboard(shipments=details, summary={"total": len(details),
                "high_risk": sum(d.prediction.risk == "high" or d.current_route.missed_connection for d in details),
                "co2_kg": sum(d.current_route.co2_kg for d in details)})

    @app.get("/api/summary", response_model=CorridorSummary, dependencies=authenticated)
    def corridor_summary(weeks: Literal["4", "13"] = "4"):
        # Other values hit the 422 handler above, which never echoes the input.
        with lock:
            details = [detail(sid) for sid in app.state.shipments]
        return summary.build_summary(app.state.catalog["history"], details,
                                     app.state.catalog["ports"], int(weeks))

    @app.get("/api/shipments/{shipment_id}", response_model=ShipmentDetail, dependencies=authenticated)
    def get_detail(shipment_id: ShipmentId):
        with lock:
            return detail(shipment_id)

    @app.post("/api/shipments/{shipment_id}/scenario", response_model=ShipmentDetail)
    def scenario(shipment_id: ShipmentId, body: ScenarioRequest, user=Depends(broker)):
        with lock:
            shipment(shipment_id)
            app.state.scenarios[shipment_id] = body.kind
            app.state.selected.pop(shipment_id, None)
            return detail(shipment_id)

    @app.post("/api/shipments/{shipment_id}/scenario/reset", response_model=ShipmentDetail)
    def reset(shipment_id: ShipmentId, user=Depends(broker)):
        # Role is enforced by this route's own Depends(broker); scenario() is
        # called as a plain function here and does not re-check it.
        return scenario(shipment_id, ScenarioRequest(kind="none"), user)

    @app.post("/api/shipments/{shipment_id}/route", response_model=ShipmentDetail)
    def choose(shipment_id: ShipmentId, body: ChooseRouteRequest, user=Depends(broker)):
        with lock:
            fresh = detail(shipment_id)
            choices = [fresh.current_route, *fresh.alternatives]
            if not any(r.id == body.route_id and r.feasible for r in choices):
                raise HTTPException(409, "Route is no longer feasible; refresh alternatives")
            app.state.selected[shipment_id] = body.route_id
            return detail(shipment_id)

    @app.get("/api/preclearance/ruleset", response_model=Ruleset, dependencies=authenticated)
    def ruleset():
        return preclearance.ruleset()

    @app.get("/api/shipments/{shipment_id}/preclearance", response_model=Declaration | None,
             dependencies=authenticated)
    def declaration(shipment_id: ShipmentId):
        shipment(shipment_id)
        return preclearance.get_declaration(app.state.db, shipment_id)

    @app.put("/api/shipments/{shipment_id}/preclearance", response_model=Declaration)
    def save(shipment_id: ShipmentId, body: DeclarationInput, user=Depends(broker)):
        with lock:
            item = shipment(shipment_id)
            with _domain_errors():
                return preclearance.save_declaration(app.state.db, item, body.model_dump(), user, SIMULATION_NOW)

    @app.post("/api/shipments/{shipment_id}/preclearance/validate", response_model=Declaration)
    def validate(shipment_id: ShipmentId, user=Depends(broker)):
        with lock:
            item = shipment(shipment_id)
            with _domain_errors():
                return preclearance.validate_declaration(app.state.db, item, user, SIMULATION_NOW)

    @app.post("/api/shipments/{shipment_id}/preclearance/submit", response_model=Declaration)
    def submit(shipment_id: ShipmentId, idempotency_key: Annotated[IdempotencyKey, Header()],user=Depends(broker)):
        with lock:
            item = shipment(shipment_id)
            with _domain_errors():
                return preclearance.submit_declaration(app.state.db, shipment_id, idempotency_key, user, SIMULATION_NOW, item)

    @app.post("/api/shipments/{shipment_id}/preclearance/review", response_model=Declaration)
    def review(shipment_id: ShipmentId, body: ReviewRequest, user=Depends(actor)):
        with lock:
            shipment(shipment_id)
            with _domain_errors():
                return preclearance.review_declaration(app.state.db, shipment_id, body.decision, body.comment, user, SIMULATION_NOW)

    @app.get("/api/shipments/{shipment_id}/audit", response_model=list[AuditEntry], dependencies=authenticated)
    def audit(shipment_id: ShipmentId):
        shipment(shipment_id)
        return preclearance.get_audit(app.state.db, shipment_id)

    @app.get("/api/shipments/{shipment_id}/audit/verify", response_model=AuditVerification,
             dependencies=authenticated)
    def verify(shipment_id: ShipmentId):
        shipment(shipment_id)
        return preclearance.verify_audit(app.state.db, shipment_id)

    @app.get("/api/shipments/{shipment_id}/ledger", response_model=LedgerStatus, dependencies=authenticated)
    def ledger_view(shipment_id: ShipmentId):
        shipment(shipment_id)
        return preclearance.ledger_status(app.state.db, shipment_id)

    @app.get("/api/hubs/forecast", response_model=HubForecast, dependencies=authenticated)
    def hub_forecast(shipment_id: Annotated[ShipmentId | None, Query()] = None):
        # Every shipment's own scenario is already in its route (detail()); the
        # requested shipment's scenario is also the regional condition (hubs.py).
        with lock:
            if shipment_id is not None:
                shipment(shipment_id)
            details = [detail(sid).model_dump() for sid in app.state.shipments]
            regional = routing.scenario_profile(
                app.state.scenarios.get(shipment_id, "none") if shipment_id else "none")
            return hubs.forecast(app.state.catalog["ports"], app.state.catalog["services"], details,
                                 regional, SIMULATION_NOW, focus_shipment_id=shipment_id)

    return app


app = create_app()
