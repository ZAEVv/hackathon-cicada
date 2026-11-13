# Case 2 only: runnable hackathon MVP

## Frozen decisions

Python 3.11+, FastAPI, Pydantic 2, SQLite (stdlib sqlite3), numpy,
scikit-learn GradientBoosting, NetworkX; React + Vite + TypeScript.
No blockchain, external LLM, live AIS, Docker dependency, or mandatory network calls.
`docs/CONTRACTS.md` and `backend/app/schemas.py` are the shared frozen interface.
Domain modules exchange JSON-compatible dictionaries; schemas validate at the REST boundary.

## Exclusive ownership (no overlapping edits)

| Owner | Owned files | Deliverable |
|---|---|---|
| Architect | `docs/CONTRACTS.md`, `docs/PLAN.md`, `backend/app/schemas.py` | contracts only |
| Data | `backend/app/data.py`, `tests/test_data.py` | catalogs, three shipments, seeded chronological history, weather fallback |
| Predictive | `backend/app/predictive.py`, `tests/test_predictive.py` | baseline + GBM, held-out evaluation, deterministic prediction |
| Routing + ESG | `backend/app/routing.py`, `backend/app/esg.py`, `tests/test_routing.py`, `tests/test_esg.py` | schedule feasibility, disruption profiles, alternatives, transparent CO2 |
| Pre-clearance | `backend/app/preclearance.py`, `tests/test_preclearance.py` | SQLite repository, validation, versioning, idempotency, hash audit |
| Frontend | `frontend/**` | responsive dashboard/detail, route diagram/map, scenarios, route choice, document form, audit |
| Lead / integration / QA | `backend/app/main.py`, package init files, `requirements.txt`, `README.md`, `.gitignore`, `scripts/**`, `tests/test_api.py`, `tests/test_integration_review.py`, other deployment files | REST orchestration, auth, CORS, launch, reproducible demo, end-to-end tests |

Ask lead before changing another owner's interface. Owners can implement against signatures
without waiting for imports: other domain modules must never be imported at module import
time except schemas; routing may import esg. Lead wires dependencies explicitly.

## Integration order

1. Data + ML + routing/ESG + pre-clearance + frontend in parallel against contracts.
2. Lead wires startup once: generate data, fit model, initialize SQLite; no training per request.
3. Wire GET detail composition and scenario overlays; then authenticated mutations.
4. Frontend connects `/api` (Vite proxy to 8000), no separate mock API in final demo.
5. Run pytest and frontend build, rehearse the exact demo below with internet disabled.

## Reproducible acceptance demo

Fresh SQLite path, seed 42, simulated clock `2026-11-12T08:00:00Z`.
Start API and frontend using README commands; both tokens explicitly labeled demo secrets.

1. Dashboard shows SCT-001, SCT-002, SCT-003; all data tagged synthetic.
2. Open SCT-001: normal planned port arrival 10:00, ready before 14:00 cutoff,
   sea departure 18:00; plan, forecast and actual remain separate.
3. Apply `storm`: readiness exceeds cutoff; planned route infeasible, original
   departure missed, ETA moves to next sailing. Show feasible next sailing and
   alternate-port route with cost, ETA, kg CO2e. No teleportation or past departures.
4. Choose a feasible alternative. Compare against baseline, with demo-not-booking label.
5. Save declaration with packing-list mass mismatch; validation shows exact field error.
   Save corrected version, validate, submit with idempotency key; retry returns same result.
6. Reviewer token records `accepted`; audit verifies. Never label this customs release.
7. Reset scenario: baseline forecasts return and route choice clears; documents/audit survive.

## Gates

- Same seed produces byte-equivalent catalogs/history and stable numeric predictions.
- Chronological holdout, training-only median baseline, synthetic-quality disclaimer.
- A missed cutoff cannot appear as feasible; sea closure does not affect inland edges.
- CO2 hand calculation matches mass_kg / 1000 * km * factor.
- Missing/invalid bearer => 401, wrong role => 403, not found => 404, conflict => 409.
- Invalid form => 422 or persisted validation_failed; repeat submit causes no duplicate audit.
- Modified audit payload is detected; restart preserves declarations and audit.
- Frontend handles loading/error/empty alternatives; buttons actually call API.

## Scope and honesty

Synthetic services are not claims about real scheduled connections. Coordinates are illustrative
port locations; weather defaults to seeded synthetic values. Optional cached Open-Meteo data
must identify source and freshness, and must not change the reproducible demo. Factors are
illustrative, not certified GLEC/ISO results. Hashes detect alteration, not truth of documents;
local demo bearer roles are not production identity/security or legal customs integration.
