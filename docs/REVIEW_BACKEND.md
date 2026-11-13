# Backend review — Smart Caspian Transit (case 2)

Independent review of the backend owner modules against `docs/CONTRACTS.md`.

- **Reviewed:** `docs/CONTRACTS.md`, `backend/app/main.py`, `backend/app/routing.py`,
  `backend/app/preclearance.py`, `backend/app/esg.py`, `backend/app/schemas.py`,
  `backend/app/data.py`, `backend/app/predictive.py`, and every file under `tests/`
  (including the new `tests/test_integration_review.py`).
- **Not reviewed (by instruction):** the still-in-progress `data`/`predictive`/`frontend`
  partial implementations and any missing partial files. Frontend integration risk is
  noted only where a backend value could plausibly couple to it, and is marked speculative.
- **Method:** static read of every line of the reviewed modules plus direct execution via
  the project `.venv` (Python 3.15.2): `pytest` (106 passed) and ad-hoc `TestClient` /
  domain-module probes. No source code was modified.
- **Verdict:** no crash, security or hard contract-violation blockers found. The findings
  below are demo-correctness and edge-case issues, ordered by severity.

---

## Already resolved (lead fixes — confirmed by regression tests)

These were identified during review and are fixed in the current `main.py`; each is pinned
by a test in `tests/test_integration_review.py` / `tests/test_api.py`.

1. **Route selection no longer drops a feasible original route.**
   `main.py:85-86` now scans `[routes["current_route"], *routes["alternatives"]]` instead of
   only `alternatives`, so selecting the currently-feasible planned route retains
   `selected_route_id`. Covered by `test_api.py::test_auth_route_scenarios_and_selection_refresh_reset`.

2. **Document-readiness delay no longer shifts the original planned sailing.**
   `main.py:69-82` computes the forecast from the document-delayed copy (`item`) but passes a
   fresh, immutable copy (`shipment(shipment_id)`) into `compute_routes`, so the baseline
   stays on the seeded `documents_ready_at` while the *actual* schedule still reflects the
   delay via `prediction.ready_at`. Covered by
   `test_integration_review.py::test_document_delay_does_not_move_original_plan_and_events_follow_route`.

3. **Future event estimates are recomputed from scenario + route.**
   `main.py:96-114` refreshes `estimated_at` for `port_arrival` / `sea_departure` / `delivery`
   while preserving `planned_at` and `actual_at`. Covered by
   `test_integration_review.py::test_storm_arrival_event_uses_scenario_while_actual_departure_is_preserved`.

---

## Findings

### 1. [Medium — confirmed] SCT-002 and SCT-003 are `missed_connection=true` in the default "none" scenario

**Location:** `backend/app/routing.py:84-92` (baseline/actual/`missed` comparison),
`backend/app/data.py:280-300` (SCT-002 `observed_delay_hours=0.5`),
`backend/app/data.py:335-355` (SCT-003).

**What happens:** with no scenario applied, the dashboard already reports
`summary.high_risk = 2` of 3, because `current_route.missed_connection` is `True` for
SCT-002 and SCT-003 (and `current_route.feasible` is `False` for both).

**Reproduction:**

```bash
cd /home/mamai/Projects/smart-caspian-transit
.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from backend.app.main import create_app
import tempfile, os
with TestClient(create_app(os.path.join(tempfile.mkdtemp(), "x.sqlite3"))) as c:
    d = c.get("/api/dashboard").json()
    print(d["summary"])  # {'total': 3, 'high_risk': 2, 'co2_kg': ...}
    for s in d["shipments"]:
        print(s["shipment"]["id"], s["current_route"]["missed_connection"],
              s["prediction"]["risk"], s["shipment"]["observed_delay_hours"])
PY
```

Output: `SCT-001 False low 0.0`, `SCT-002 True low 0.5`, `SCT-003 True low 0.0`.

**Root cause.** `missed` is computed as a departure shift between two schedules:

- `baseline` uses `planned_ready = max(planned_port_arrival + queue_hours + 1, documents_ready_at)`
  (`routing.py:85-86`) — it excludes both `observed_delay_hours` and the ML `delay_hours`.
- `unblocked` uses `prediction.ready_at`, which *does* include `observed_delay_hours` and the
  ML `delay_hours` (`predictive.py:162-164`).

Two independent drivers:

- **SCT-002:** `observed_delay_hours=0.5` is a known, already-incurred delay. It is correctly
  added to readiness (contract line 74) but absent from the "originally planned" baseline, so
  the 18:00 → 19:00 `transfer-olya` shift is permanent and `missed` is always true.
- **SCT-003:** `observed_delay_hours=0.0`, but the ML predicts `delay_hours≈0.32`. Its planned
  road transfer (`transfer-olya`, hourly, `cutoff_hours=0.5`) lands exactly on the cutoff
  boundary: `planned_ready = 08:00 + 0.5 queue + 1 = 09:30`, so the 10:00 slot is only just
  feasible (`09:30 + 0.5 == 10:00`). Any predicted delay > 0 flips the slot to 11:00 and sets
  `missed=true`.

**Impact.** The calm baseline demo shows 2 of 3 shipments as "connection missed / infeasible",
which undercuts the intended calm-vs-storm contrast (`docs/DEMO.md` lines 22-23) and makes
the `high_risk` dashboard KPI misleading. The storm scenario still works, but the dramatic
difference is partially pre-empted.

**Assessment.** Semantically the flag is "correct" (a 0.5 h known delay *did* miss the original
plan; a 0.32 h predicted delay *does* miss a zero-slack road slot), so this is a
demo-data/scheduling-sensitivity issue rather than a scheduling-logic defect. It is the most
substantive outstanding issue because it is user-visible on the default screen.

**Recommended fix (data tuning, no logic change):**
- Give SCT-003 more slack so its planned `transfer-olya` departure is not exactly on the cutoff
  boundary (e.g. shift `planned_port_arrival` earlier, or document that a small ML delay can
  legitimately miss a tight road slot), and
- either give SCT-002 `observed_delay_hours=0` (keep the demo "calm") or accept and document
  that SCT-002 is a permanently-missed example. The alternative is to confirm the semantics of
  `observed_delay_hours` with the product owner: if it is already reflected in the seed
  `estimated_at` (SCT-002's port_arrival shows 15:00 planned / 15:30 estimated), then flagging
  the miss is expected — but the dashboard wording should not call it "high risk".

---

### 2. [Low — confirmed] Event `source` is rewritten to an undocumented value `"synthetic-model"`

**Location:** `backend/app/main.py:113`.

The timeline recompute sets `event["source"] = "synthetic-model"` for every non-`actual`
event. `schemas.Event.source` is a plain `str`, so no schema error is raised, but
`docs/CONTRACTS.md` (line 219) documents `"source":"synthetic"` for events and no other value.
The seed data (`data.py`) uses `"synthetic"`.

**Reproduction:** `GET /api/shipments/SCT-001` → any forecast event has
`"source": "synthetic-model"` (observed `rail_departure` stays `"synthetic"` because it has an
`actual_at`).

**Impact/speculative:** if the frontend maps `source` to a badge/label, `"synthetic-model"`
may render as an unknown value. Confirm with the frontend or change to `"synthetic"` (or add
`"synthetic-model"` to the contract's documented vocabulary).

**Recommended fix:** keep `source` as `"synthetic"` (the `quality_flag` already distinguishes
`forecast` vs `observed`), or document the new literal.

---

### 3. [Low — confirmed] Timeline recompute omits `road_departure` / `rail_departure` and is fragile for re-routed sea legs

**Location:** `backend/app/main.py:100-114`.

`detail()` only recomputes `estimated_at` for `port_arrival`, `sea_departure`, and `delivery`.
Other event types fall through unchanged:

- SCT-002's `evt-002-3` is `road_departure` (data.py:322). Under any scenario (e.g. storm) its
  `estimated_at` stays frozen at the seeded `2026-11-12T19:00:00Z` even though the scheduled
  road transfer actually moves (`transfer-olya@13:00` in storm, from the routing output).
- `sea_departure` matching uses `mode == "sea" and leg.origin_id == event.location_id`
  (`main.py:106-107`). If the user selects an alternative that routes the sea leg from a
  different port, `matching` is `None` and the event's `estimated_at` becomes `null` rather than
  tracking the actual selected sea leg. (Demo path: SCT-003's `sea_departure` event is at
  `olya`; selecting a `sea-main` alternative would null it.)

**Reproduction (SCT-002 road_departure):**

```bash
.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from backend.app.main import create_app
import tempfile, os
with TestClient(create_app(os.path.join(tempfile.mkdtemp(), "x.sqlite3"))) as c:
    h = {"Authorization": "Bearer dev-broker-token"}
    after = c.post("/api/shipments/SCT-002/scenario", json={"kind":"storm"}, headers=h).json()
    ev = {e["event_type"]: e for e in after["shipment"]["events"]}
    print("road_departure estimated_at:", ev["road_departure"]["estimated_at"])  # unchanged
    print("first leg departure:", after["current_route"]["legs"][0]["departure_at"])
PY
```

**Impact:** the timeline's forecast column diverges from the route/schedule shown beside it for
the road and rail departure rows; minor display inconsistency.

**Recommended fix:** generalize the `sea_departure` branch to a `*_departure` matcher keyed on
the leg's origin location (and mode) for `road` and `rail` as well, and fall back to the
selected route's corresponding leg rather than `None`.

---

### 4. [Low — confirmed] `verify_audit` reports `valid=true` for a shipment with no declaration

**Location:** `backend/app/preclearance.py:183-211`.

For a shipment that has never been saved (no `declarations` row and no `audit_entries`), the
loop body never runs, `invalid` stays `None`, and the guard
`if invalid is None and declaration is not None` is false, so the response is
`{"valid": true, "entries_checked": 0, "first_invalid_sequence": null}`.

**Reproduction:** fresh DB → `GET /api/shipments/SCT-001/audit/verify` (no prior PUT) returns
`valid: true`.

**Impact:** the frontend "Verify" button would render a green "valid" state for a shipment that
has no audit trail at all, which is misleading.

**Recommended fix:** distinguish the empty case, e.g. return
`valid: false` with `first_invalid_sequence: null` (or a dedicated `"nothing to verify"` flag)
when there is no declaration row. This is a presentation/contract-clarity change, not a
hash-integrity defect (a shipment *with* a declaration but a missing audit entry is correctly
flagged by `latest is None`).

---

### 5. [Low — speculative] `port_arrival` forecast overwrites the seeded estimate in "none"

**Location:** `backend/app/main.py:98-104` vs `backend/app/data.py:259-262`.

`detail()` recomputes `port_arrival.estimated_at = planned_port_arrival + observed_delay +
scenario.arrival_delay`. For SCT-001 in "none" this yields `10:00:00Z`, overwriting the seeded
forecast `10:30:00Z` from `data.py` (which, combined with `observed_delay_hours=0`, is itself
internally inconsistent). This is a byproduct of the (correct) recompute; the seed's 30-minute
forecast delta is lost.

**Impact/speculative:** cosmetic; only visible if the UI or a test expects the seeded `10:30`.
Confirm whether the `10:30` seeded estimate should instead be encoded as `observed_delay_hours`
or left as-is.

**Recommended fix:** align the seed `estimated_at` with `observed_delay_hours` (or document
that `estimated_at` is always recomputed and the seed value is illustrative).

---

## Notes and review limits

- **Not verified:** live Open-Meteo network path (`data.get_weather(use_live=True)`), and any
  frontend rendering. These are out of scope here.
- **Confirmed clean:** ESG (`esg.py`) recomputes `co2_kg` from mode+distance, ignores stale
  fields, and matches the 942 kg contract example; the audit hash-chain covers
  sequence/previous-hash/content-hash and detects the tampering cases in
  `tests/test_preclearance.py::test_tamper_detection`; idempotent submit replays before the
  state check and survives post-review retries; scenario profiles match the contract table;
  route IDs are stable (service@departure, not list index) and de-duplicated.
- No dependency changes were made; `.venv` was left untouched.
