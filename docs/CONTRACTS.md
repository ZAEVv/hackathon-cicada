# Frozen contracts v1 — Smart Caspian Transit, case 2

## Conventions

Source of field truth: `backend/app/schemas.py` (Pydantic 2). Below, `dict` means
JSON-compatible `dict[str, Any]` with the named schema's fields, NOT a Pydantic instance.
Functions return dictionaries; main validates them using schemas. No domain module imports
main or another domain module, except routing -> esg. No import-time training, DB or network I/O.
All datetimes are UTC ISO strings ending in `Z`; parse as timezone-aware UTC.
Clock is fixed at `2026-11-12T08:00:00Z`, seed is 42. Never use wall-clock for domain calculations.
Mass is kg in shipment/documents, capacity tonnes (1000 kg), distances km, duration hours,
money USD, emissions kg CO2e. Round display only, retain calculation precision internally.
Errors: functions raise ValueError for invalid state/input, KeyError for unknown IDs,
PermissionError for forbidden role; main maps to 409/404/403. Pydantic input failures => 422.

## Data owner: `backend/app/data.py`

```python
def load_demo_data(seed: int = 42) -> dict: ...
# {"ports": list[Port dict], "services": list[Service dict],
#  "shipments": list[Shipment dict], "history": list[dict]}
def get_weather(port_id: str, *, use_live: bool = False,
                cache_path: str | None = None) -> dict: ...  # Weather
```

Always include shipments SCT-001..SCT-006 (004-006 run on the eastern branch); IDs and generated order stable. Locations:
`rail_hub` (Russian inland terminal), `makhachkala`, `olya`, `anzali`, `tehran`, plus the
eastern branch `aktau` (KZ), `turkmenbashi` (TM) and `amirabad` (IR).
Use human-readable names/coordinates in Port. Service IDs must include
`rail-main` (rail_hub -> makhachkala), `sea-main` (makhachkala -> anzali),
`transfer-olya` (makhachkala -> olya road), `sea-olya` (olya -> anzali),
`road-final` (anzali -> tehran). Extra services allowed; no changes to required IDs. Illustrative eastern-branch services:
`rail-east` (rail_hub -> aktau), `sea-aktau` (aktau -> amirabad), `rail-kz-tm`
(aktau -> turkmenbashi), `sea-tkm` (turkmenbashi -> amirabad), `sea-mak-amirabad`
(makhachkala -> amirabad, the eastern alternative when `closure` blocks `sea-main`) and
`rail-amirabad` (amirabad -> tehran). SCT-004 (rail-east, sea-aktau, rail-amirabad),
SCT-005 (sea-mak-amirabad, rail-amirabad) and SCT-006 (rail-east, rail-kz-tm, sea-tkm,
rail-amirabad) are synthetic and every new port has deterministic synthetic weather.
Road services use explicit departures too (hourly is fine), not magical instantaneous edges.

SCT-001 is already inbound on rail-main, current_location_id=makhachkala for the
remaining-route solver (means next handling location, not assertion that arrival happened).
planned_service_ids includes all three original legs; solver skips the completed/inbound
rail leg and starts at predicted port readiness. planned_port_arrival=Nov 12 10:00Z,
documents_ready_at=Nov 12 11:00Z, queue_hours=0.5, observed_delay_hours=0.
Sea-main first departure Nov 12 18:00Z, cutoff_hours=4, handling_hours=1;
following departures Nov 13/14/15 at 18:00Z. Sea-olya daily 22:00Z, cutoff_hours=2;
transfer service should permit a feasible later alternate under storm. Catalog horizon
at least 7 days. Capacities at least 40 tonnes and all demo cargo general, 20,000 kg.
Use plausible illustrative durations/distances/costs; never claim real service availability.

History: at least 600 independent shipment rows over 90 days strictly before simulation_now,
sorted by `observed_at`, one row per shipment (no grouping leakage). Exact row keys:
`shipment_id: str`, `observed_at: str`, `queue_hours: float`, `wind_mps: float`,
`wave_height_m: float` (seeded, correlated with wind; penalises delay above ~1.7 m),
`precipitation_mm: float`, `observed_delay_hours: float`, `mass_tonnes: float`,
`hours_to_cutoff: float`, `delay_hours: float`, `missed_connection: int` (0/1).
First seven numeric fields except labels are known at prediction time. `delay_hours` is
additional arrival/processing delay beyond known observed_delay and queue; target >= 0.
Use seeded noise and nonlinear weather/congestion effects. Low-wind unloaded SCT-001
must predict small delay (<2 hours) and storm must miss the cutoff independently of ML noise.
Weather defaults deterministic synthetic, no network. Optional live path has short timeout,
cache source/freshness flags and catches unavailable/null values; never required for demo.

## Predictive owner: `backend/app/predictive.py`

```python
def train_model(history: list[dict], seed: int = 42) -> tuple[object, dict]: ...
# opaque in-memory bundle, ModelMetrics dict
def predict(model: object, shipment: dict, weather: dict,
            scenario: dict, now: str = "2026-11-12T08:00:00Z") -> dict: ... # Prediction
```

Chronological first 80% train / last 20% test, no shuffle. One monotonic
HistGradientBoosting regressor (non-decreasing in queue, wind, precipitation, known
delay and wave height; mean fallback below 20 training rows). There is no separate classifier.
Train-only median delay baseline, MAE both; ROC AUC of `miss_probability` against holdout
`missed_connection` if the test set has both classes, else null. No future actual
timestamps as features. Feature order is queue_hours, wind_mps, precipitation_mm,
observed_delay_hours, mass_tonnes, wave_height_m (history rows lacking it fall
back to the wind estimate). `hours_to_cutoff` stays in history (remaining slack
beyond baseline arrival plus one hour handling) but only scores holdout labels; it is not
a delay driver. The synthetic generator labels observed_delay + queue + additional_delay >
slack as missed; equality is accepted, matching scheduling readiness <= cutoff.
Scenario arrival delay augments observed_delay; queue_extra augments queue; wind override applies; a scenario
`wave_height_m` override applies, else weather wave height, else (also after a wind-only override)
`estimate_wave_height_m(wind)` = min(8, 0.2 + 0.009*wind^2) m. Factors: "High sea state"
(>= 3 m), "Moderate sea state" (>= 1.5 m).
`delay_hours` is predicted additional delay; readiness formula:
`max(planned_port_arrival + observed_delay + scenario.arrival_delay + queue_hours
+ scenario.queue_extra + 1 hour handling + delay_hours, documents_ready_at)`.
No double counting observed delay in predicted target.

Cutoff: `predict(..., cutoff=routing.planned_cutoff(original_shipment, services, now))`,
i.e. the cutoff of the first originally planned departure, the same departure the route
baseline measures `missed_connection` against (SCT-001: sea-main 18:00Z - 4h = 14:00Z;
SCT-002: transfer-olya 18:00Z - 0.5h = 17:30Z). Without a cutoff, the arrival day's 14:00Z
is assumed. The prediction clock does not change the forecast.

`miss_probability` = share of forecast errors (residuals of the last quarter of training
rows, scored by a model fitted on the rows before them) for which delay_hours + error
would push readiness past the cutoff. Documents ready after the cutoff give probability 1.
So probability, ready_at and route feasibility cannot contradict each other. It is a
synthetic estimate, not a guaranteed calibrated probability; `risk` low <.35, medium <.7,
high otherwise. Factors also identify the synthetic, not-guaranteed calibration status;
they are deterministic input explanations, not fabricated SHAP.

## Routing / ESG owner

```python
# routing.py
def scenario_profile(kind: str) -> dict: ... # Scenario
def compute_routes(shipment: dict, ports: list[dict], services: list[dict],
                   prediction: dict, scenario: dict,
                   now: str = "2026-11-12T08:00:00Z") -> dict: ...
# {"current_route": Route dict, "alternatives": list[Route dict]}
# esg.py
def calculate_emissions(legs: list[dict], mass_kg: float) -> dict: ... # Emissions
```

Profiles: none all zero; storm arrival_delay_hours=3, queue_extra_hours=2, wind_mps=22, wave_height_m=4.5;
congestion arrival_delay_hours=0, queue_extra_hours=6; closure blocks `sea-main` only.
Storm is a local disruption at the shipment's current port (Makhachkala on the western line, Aktau on
the eastern branch), NOT a claim all Caspian ports remain safe in storms.
Other wind and wave_height_m values null. Pure functions; do not mutate inputs. NetworkX directed graph
enumerates small simple paths, scheduler selects departures >= readiness+handling before cutoff.
Start at current_location_id at prediction.ready_at (initial handling already counted);
for subsequent edges add the NEXT service's handling_hours to preceding arrival before cutoff.
Require ready <= departure-cutoff, capacity >= mass_kg/1000, supported cargo,
not blocked and departure >= now. Wait for next scheduled departure when first missed.
Cost is fixed per shipment per service, not multiplied by mass. Route includes only remaining
legs; emissions/cost scope clearly labeled remaining journey in frontend.

`current_route` uses original remaining path and earliest originally planned departure;
if original departure missed, set feasible=false/missed_connection=true and reason, but legs/eta
show the earliest feasible *recovery* sailing on that same path (or eta=null/legs=[] if blocked).
Alternatives contain at most 3 feasible choices: next-departure recovery and alternate port path.
Each gets a stable ID incorporating service IDs and departure timestamps (not list index),
sorted by ETA then cost then ID. Deduplicate. All Route totals agree with legs and ESG output.
Route choice is local simulated selection, never booking or capacity reservation.
Main displays selected alternative as current_route after selection while retaining original
comparison in alternatives where useful; main must recompute and verify ID on every selection.

ESG factors kg CO2e/(tonne km): rail .022, road .105, sea .016; factor_version demo-v1.
Each leg = mass_kg/1000 * distance_km * factor. All factors illustrative, transport-only;
exclude terminals/storage/empty repositioning, no certified WTW or GLEC/ISO compliance claim.

## Pre-clearance owner: `backend/app/preclearance.py`

```python
def init_db(db_path: str) -> None: ...
def get_declaration(db_path: str, shipment_id: str) -> dict | None: ... # Declaration
def save_declaration(db_path: str, shipment: dict, payload: dict,
                     actor: dict, now: str) -> dict: ... # Declaration
def validate_declaration(db_path: str, shipment: dict,
                         actor: dict, now: str) -> dict: ... # Declaration
def submit_declaration(db_path: str, shipment_id: str, idempotency_key: str,
                       actor: dict, now: str) -> dict: ... # Declaration
def review_declaration(db_path: str, shipment_id: str, decision: str,
                       comment: str, actor: dict, now: str) -> dict: ... # Declaration
def get_audit(db_path: str, shipment_id: str) -> list[dict]: ... # AuditEntry[]
def verify_audit(db_path: str, shipment_id: str) -> dict: ... # AuditVerification
```

Actor is trusted server-created `{"id":"demo-broker","role":"broker"}` or
`{"id":"demo-reviewer","role":"reviewer"}`; never accept actor from request JSON.
Broker saves/validates/submits; reviewer only reviews. Both see all three synthetic shipments;
no claim of production tenant isolation. Functions enforce roles as defense in depth.
One stable declaration ID per shipment, save increments version. Save draft even when
business-invalid, resets validation and status; submitted cannot edit until reviewer requests
revision. Accepted edits allowed but create new draft version and invalidate acceptance.
Validate requires nonblank fields, two-letter origin country, numeric 6-10 digit HS code,
all three distinct document kinds with nonblank numbers, matching container IDs and
mass tolerance <=1 kg across shipment/declaration/all docs. Issue codes:
required, invalid_format, missing_document, duplicate_document, mass_mismatch, container_mismatch.
Validation persists ready or validation_failed and issues; submit allowed only ready.
Reviewer allowed submitted -> demo_accepted or needs_revision only. No automatic real approval.

Submit idempotency stored transactionally under (actor.id, shipment_id, key), along with
version/content hash and response JSON. Same key/same version returns original response without
new audit; same key after document revision => ValueError conflict. Check replay BEFORE state
check, so retry after review still returns original submitted response. SQLite transactions
atomically change declaration and append audit; parameterized SQL, connection per function.

Every change logs sequence (per declaration starting 1), action, actor ID, fixed at time,
payload (version/status/content_hash and decision or validation as appropriate), previous_hash
(64 zeros at genesis) and hash. Content hash SHA256 canonical JSON of DeclarationInput.
Entry hash SHA256 canonical JSON of all AuditEntry fields EXCEPT hash, including previous_hash.
Canonical JSON = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).
verify checks order, previous linkage and recomputed hashes; also current declaration payload
hash equals stored content_hash and latest audit content_hash. Verification detects ordinary
tampering only; no external trust anchor, not cryptographic signature or proof of legal validity.

## Integration REST owner: `backend/app/main.py`

FastAPI `app`, CORS localhost:5173, Vite proxies /api to localhost:8000.
All responses application/json, unwrapped schema objects unless table says otherwise.
Every /api route except GET /api/health requires a bearer token (reads accept either role);
missing/invalid token => 401 with `WWW-Authenticate: Bearer`, wrong role => 403.
Environment `DEMO_BROKER_TOKEN` default `demo-broker-token`, `DEMO_REVIEWER_TOKEN`
default `demo-reviewer-token`; bind localhost by default. The built-in defaults are honoured
only for loopback clients without forwarding headers (`Forwarded`, `X-Forwarded-For`,
`X-Real-IP`) and never when `DEMO_DISABLE_DEFAULT_TOKENS=1`; identical tokens => 401 for both.
Security layer (`backend/app/security.py`): 400 bad Host, 403 cross-site browser request,
405 other methods, 413 body over `MAX_BODY_BYTES`, 429 + Retry-After (rate limit / 20 failed
token guesses), 422 never echoes the rejected input, unexpected errors => 500
{"detail":"Internal server error"}. See README «Безопасность».
No role header; token lookup determines role. SQLite `DATABASE_PATH` defaults
`./smart_caspian_demo.sqlite3`. Simulation state lives in main per shipment (not global per user);
reset on process restart, documented single-user demo. Documents/audit persist.

| Method/path | Body | Return | Role |
|---|---|---|---|
| GET /api/health | — | {"status":"ok","simulation_now":"...","demo_only":true} | public |
| GET /api/catalog | — | {"ports":Port[],"services":Service[]} | broker or reviewer |
| GET /api/dashboard | — | Dashboard (summary total, high_risk, co2_kg) | broker or reviewer |
| GET /api/shipments/{id} | — | ShipmentDetail | broker or reviewer |
| GET /api/model/metrics | — | ModelMetrics | broker or reviewer |
| POST /api/shipments/{id}/scenario | ScenarioRequest | ShipmentDetail | broker |
| POST /api/shipments/{id}/scenario/reset | none | ShipmentDetail | broker |
| POST /api/shipments/{id}/route | ChooseRouteRequest | ShipmentDetail | broker |
| GET /api/shipments/{id}/preclearance | — | Declaration or null | broker or reviewer |
| PUT /api/shipments/{id}/preclearance | DeclarationInput | Declaration | broker |
| POST /api/shipments/{id}/preclearance/validate | none | Declaration | broker |
| POST /api/shipments/{id}/preclearance/submit | none, Idempotency-Key header required | Declaration | broker |
| POST /api/shipments/{id}/preclearance/review | ReviewRequest | Declaration | reviewer |
| GET /api/shipments/{id}/audit | — | AuditEntry[] | broker or reviewer |
| GET /api/shipments/{id}/audit/verify | — | AuditVerification | broker or reviewer |

Every successful mutation returns 200. No declaration for validate/submit => 404;
unknown shipment => 404. Missing Idempotency-Key => 422. Errors FastAPI {"detail":...}.
Scenario apply REPLACES profile, never stacks; apply/reset clears route selection only.
Main snapshots original shipment, overlays weather via scenario, passes profile to predict;
do not add arrival/queue effects twice. Documents readiness: if latest declaration exists
and is not ready/submitted/demo_accepted, main sets documents_ready_at=max(original,
simulation_now+12h); validated-ready or accepted restores original timestamp. No declaration
uses seeded timestamp. Always recompute after document changes so routing reflects readiness.

## Full frontend payload example

Illustrative schema-complete GET shipment detail; actual ML numbers may differ but stay seeded.
Routes here cover remaining legs only. Frontend must not hardcode these numeric values.

```json
{
  "simulation_now":"2026-11-12T08:00:00Z","synthetic":true,
  "shipment":{
    "id":"SCT-001","reference":"North-South demo 001","container_id":"DEMO0000001",
    "cargo":"general","mass_kg":20000,"origin_id":"rail_hub","destination_id":"tehran",
    "current_location_id":"makhachkala","status":"in_transit",
    "planned_port_arrival":"2026-11-12T10:00:00Z","planned_delivery_at":"2026-11-14T06:00:00Z",
    "planned_service_ids":["rail-main","sea-main","road-final"],"queue_hours":0.5,
    "observed_delay_hours":0,"documents_ready_at":"2026-11-12T11:00:00Z",
    "events":[{"id":"evt-001","event_type":"port_arrival","location_id":"makhachkala",
      "planned_at":"2026-11-12T10:00:00Z","estimated_at":"2026-11-12T10:30:00Z",
      "actual_at":null,"source":"synthetic","received_at":"2026-11-12T08:00:00Z","quality_flag":"forecast"}]
  },
  "weather":{"wind_mps":6,"precipitation_mm":0,"wave_height_m":0.5,"source":"synthetic",
    "observed_at":"2026-11-12T08:00:00Z","stale":false},
  "scenario":{"kind":"none","arrival_delay_hours":0,"queue_extra_hours":0,"wind_mps":null,"wave_height_m":null,"blocked_service_ids":[]},
  "prediction":{"delay_hours":0.5,"ready_at":"2026-11-12T12:00:00Z","miss_probability":0.12,
    "risk":"low","factors":["Low wind","Short terminal queue"],"model_version":"gbm-demo-v3","data_source":"synthetic"},
  "current_route":{
    "id":"sea-main@2026-11-12T18:00:00Z+road-final@2026-11-14T00:00:00Z",
    "label":"Planned Caspian crossing","feasible":true,"reason":null,"missed_connection":false,
    "eta":"2026-11-14T06:00:00Z","cost_usd":1600,"co2_kg":942,
    "legs":[
      {"service_id":"sea-main","origin_id":"makhachkala","destination_id":"anzali","mode":"sea",
       "departure_at":"2026-11-12T18:00:00Z","arrival_at":"2026-11-13T18:00:00Z",
       "cutoff_at":"2026-11-12T14:00:00Z","distance_km":600,"cost_usd":1000,"co2_kg":192},
      {"service_id":"road-final","origin_id":"anzali","destination_id":"tehran","mode":"road",
       "departure_at":"2026-11-14T00:00:00Z","arrival_at":"2026-11-14T06:00:00Z",
       "cutoff_at":"2026-11-14T00:00:00Z","distance_km":357.14285714285717,"cost_usd":600,"co2_kg":750}
    ],
    "emissions":{"total_kg":942,"by_leg_kg":[192,750],"factor_version":"demo-v1",
      "factors_kg_per_tonne_km":{"rail":0.022,"road":0.105,"sea":0.016},
      "disclaimer":"Illustrative transport-only estimate; not ISO/GLEC certified."}
  },
  "alternatives":[],"selected_route_id":null,"preclearance":null
}
```

Valid declaration PUT (change packing_list mass to 19000 for the demo validation error):

```json
{"shipper":"Demo Russian Exporter","consignee":"Demo Iranian Importer","description":"General machine parts",
 "mass_kg":20000,"value_usd":45000,"origin_country":"RU","hs_code":"847990",
 "container_id":"DEMO0000001","documents":[
 {"kind":"invoice","number":"INV-001","container_id":"DEMO0000001","mass_kg":20000},
 {"kind":"packing_list","number":"PK-001","container_id":"DEMO0000001","mass_kg":20000},
 {"kind":"transport","number":"TR-001","container_id":"DEMO0000001","mass_kg":20000}]}
```

Frontend: dashboard cards and shipment list; detail route map/SVG using catalog coordinates,
timeline with planned/forecast/actual, readiness vs cutoff, probabilities and source labels,
scenario selector/reset, feasible alternatives choose button, remaining-leg CO2 breakdown,
editable document form (save/validate/submit/review), audit list + verify button. Token inputs
must explain both roles and demo-only access. Never use the word "released" for demo_accepted.
