# Final independent Astra review — case 2

## Verdict: CONDITIONAL PASS

All three mandatory case-2 capabilities are implemented and runnable: historical/weather/queue-based ML; role-protected simulated pre-clearance; and a logistics twin with scheduled alternatives and remaining-journey ESG. This is a credible hackathon MVP, not a missing-feature submission. **Do not declare the full interactive workflow clean until findings 1–3 are fixed.** No critical security failure or failure of the central SCT-001 happy path was found. Findings 4–6 are concrete correctness gaps worth fixing before handoff; finding 7 is outside the fixed-clock demo path.

Reviewed independently: `SOURCE_CASE_2.md`, `CONTRACTS.md`, `QA.md`, README, all backend implementation modules, frontend `main.tsx`, tests and scripts. Read the supplementary backend review after reproducing the principal findings; its no-blocker conclusion does not cover the UI and workflow failures below.

### Evidence and isolation

- Independently reran `.venv/bin/python -m pytest -q -p no:cacheprovider`: **106 passed**, one Starlette TestClient deprecation warning.
- `npm exec -- tsc --noEmit`: passed.
- `npm run build -- --outDir /tmp/opencode/astra-review-build`: TypeScript and Vite production build passed.
- Additional Python probes used `TestClient(create_app(temp_db))` with temporary SQLite under `/tmp/opencode`; none used the persistent user database.
- Targeted Chromium/Playwright UI reproduction used `/usr/bin/chromium`, the running Vite page, and intercepted **all** browser `/api/**` requests with fixtures. Only public GETs were used to obtain catalog/dashboard fixture shapes. No running-server mutation or server restart was performed.
- This review did not rerun the previously reported full browser E2E suite. The targeted UI check below was independently executed.

## Prioritized findings

### 1. High — reviewer can accept a different declaration from the form currently displayed

**Locations:** `frontend/src/main.tsx:1128–1129,1163–1164` (inputs disabled only while busy), `1267–1295` (review enabled solely by submitted status), `430–432` (response overwrites local form).

**Reproduction, confirmed in Chromium with intercepted API fixtures:**

1. Display a submitted declaration with shipper `Saved exporter`.
2. Change the editable shipper field to `UNSAVED DIFFERENT EXPORTER`.
3. The “Принять в демо” button remains enabled; click it.
4. Browser sends only `{decision: "demo_accepted", comment: ""}`. The submitted stored payload is accepted, and the visible shipper silently changes back to `Saved exporter`.

Observed output: `DIRTY_REVIEW_BUTTON_ENABLED true`; `FIELD_AFTER_ACCEPT Saved exporter`.

**Impact:** the prototype's reviewer action applies to data other than the data being inspected. The dirty-form fix protects validation/submission but not review. This is a direct issue in the required protected pre-clearance demonstration, not a request for production approval infrastructure.

**Fix:** make declaration/document fields read-only while submitted, and protect review against dirty state. Prefer displaying the exact submitted snapshot for review. Regression: submitted fields cannot be edited, or neither review action can run until the stored snapshot is restored.

### 2. Medium — revision → revalidate → submit can loop forever on a historical submission response

**Locations:** `backend/app/preclearance.py:128–138,149–155`; `frontend/src/main.tsx:1200–1201,1215–1225`.

**Reproduction, confirmed against isolated API:**

1. Save a valid declaration v1, validate, submit with key `SCT-001-v1`.
2. Reviewer returns `needs_revision`.
3. Without saving another draft, invoke validate. It returns `ready`, still v1. The UI enables this action because its form equals the stored payload.
4. Submit again with `SCT-001-v1` (the UI always generates this version-based key).
5. Response says `submitted`, but GET declaration still says `ready`; audit receives no new submission. Repeated clicks repeat the discrepancy, and review is unavailable.

**Impact:** an ordinary reachable UI lifecycle cannot progress after revision unless the user knows to save again. This is not a defect in replay-before-state-check itself: replaying the original response after review is explicitly required. The defect is allowing a new submission cycle without a new version while reusing its old key.

**Fix:** require a new saved draft/version before validating a `needs_revision` declaration, and reflect that in UI controls. Preserve existing post-review replay behavior. Test the full revision branch with the UI's version-derived key, not only the accepted→save branch.

### 3. Medium — SCT-002 storm timeline predicts road departure before readiness

**Location:** `backend/app/main.py:100–114` (only sea departures are refreshed); seed event at `backend/app/data.py:318–324`.

**Reproduction, isolated API:** POST `/api/shipments/SCT-002/scenario` with broker token and `{"kind":"storm"}`.

Observed values:

| Field | UTC |
|---|---|
| Prediction readiness | Nov 13 12:05:43.108763 |
| Scheduled `transfer-olya` departure | Nov 13 13:00 |
| Timeline `road_departure.estimated_at` | **Nov 12 19:00** |

**Impact:** the digital twin shows a departure about 18 hours before its own ready time and disagrees with the route next to it. This is more than cosmetic localization; it contradicts the event/schedule consistency claimed in QA item 3.

**Fix:** refresh future road and rail departures using the same mode/origin matching as sea departures; preserve planned/actual timestamps. Test SCT-002 and all future departure types. A missing forecast at an abandoned port after rerouting is reasonable; do not simply attach an Olya sailing to a Makhachkala event without changing its meaning/location.

### 4. Medium — non-finite declaration value passes validation and submission, violates JSON contract

**Locations:** `backend/app/schemas.py:142–158` (positive floats without finite constraint); `backend/app/preclearance.py:94–125` (no finite-value business check).

**Reproduction, isolated API:** take the valid declaration example in `CONTRACTS.md`, replace `value_usd` with the JSON string `"Infinity"`, then PUT, validate and submit with a fresh idempotency key.

Observed: PUT **200**, response `payload.value_usd: null`; validate **200 ready**; submit **200 submitted**. SQLite stores the non-finite value, while Pydantic response serialization emits null. This was not a 500 crash in the installed stack.

**Impact:** the protected validation prototype approves a declaration with an invalid monetary amount and returns a payload incompatible with its declared numeric frontend/API type. Request JSON itself can be entirely valid; no nonstandard bare `Infinity` token is necessary.

**Fix:** use finite constrained floats (`allow_inf_nan=False`, or equivalent) for declaration/document masses and values. Reject non-finite input with 422 before persistence. Test `"Infinity"`, overflowing numeric input, and all numeric fields, not just negative/zero values.

### 5. Medium — audit verification does not bind current lifecycle metadata to the latest entry

**Location:** `backend/app/preclearance.py:207–210`; comparison includes content hash but omits version/status already present in each audit payload (`62–67`).

**Reproduction, isolated SQLite only:** save/validate a declaration, then update only its stored `declaration_json.status` to `demo_accepted` and `version` to `99`, retaining its payload, hash and audit entries. GET audit verification returns `valid: true`.

**Impact:** an ordinary alteration of current approval/version state is invisible to the green integrity indicator even though the latest intact audit entry contains enough information to detect it. This does not require inventing signatures, an external ledger, or protection against a complete coherent database rewrite.

**Fix:** compare current status/version against the latest audit payload as well as content hash; consider matching the recorded update time. Add independent status/version tampering and trailing-state-event deletion tests. This exceeds the contract's narrowly specified content-hash comparison, but is a substantive gap in README's ordinary-tampering claim and the UI's current-record integrity interpretation.

### 6. Medium — advertised repeatable demo aborts from ordinary persisted workflow states

**Location:** `scripts/demo.py:43–53`.

**Reproduction, executed using the actual script with its urllib transport redirected to isolated TestClient:** leave SCT-001 in `submitted`, then run `main()`.

The script reviews it to `needs_revision`, then immediately requires the normal route to be feasible. Revision deliberately delays documents until 20:00, missing the original cutoff. It exits with **`Normal baseline route is not feasible`**. Existing draft/validation_failed/needs_revision records have the same startup problem. A fully completed previous demo run ending accepted can still be rerun successfully.

**Impact:** a presenter who has just exercised the UI or interrupted the demo cannot reliably run the advertised end-to-end script. Its attempted submitted-state recovery causes the very baseline failure that aborts execution.

**Fix:** establish a valid, validated declaration before asserting baseline when a persisted declaration blocks readiness; or explicitly run this script against a fresh isolated demo instance and document that prerequisite. Add a check starting in each lifecycle state. Do not delete audit history to reset it.

## Non-blocking temporal edge case

### 7. Low in this fixed-clock MVP — advancing routing `now` rewrites the original plan

**Location:** `backend/app/routing.py:85–91`; baseline `_schedule` receives current `now_dt`.

Direct `compute_routes` probe with original SCT-001 and prediction `ready_at=2026-11-12T20:00:00Z`:

- `now=2026-11-12T08:00:00Z`: recovery Nov 13 18:00, `feasible=false`, `missed_connection=true`.
- `now=2026-11-12T19:00:00Z`: same recovery, **`feasible=true`, `missed_connection=false`**.

Once the original Nov 12 departure is past, baseline scheduling drops it and silently promotes the recovery to the original plan. Main always uses the fixed 08:00 clock, so this is not a current exposed demo blocker. Before enabling clock progression, anchor baseline departures independently of current time; only actual/recovery schedules should filter out past departures.

## Reviewed boundaries and findings not promoted to blockers

- The supplementary review's default SCT-002/003 missed connections are consistent with the solver's original earliest-slot baseline and tight road cutoff slack. They are not evidence of invalid routing or ML failure. Data tuning is optional; do not hide real slot changes merely to make all cards green.
- The ML cutoff for other shipments is explicitly the arrival-day 14:00 demo cutoff, not necessarily their selected Olya sailing's cutoff. The current model probability must remain presented as a synthetic model estimate, separate from actual route feasibility. It is not calibrated or route-specific. No holdout fitting leakage was found; train-only median baseline, chronological split, seeded generation and single-class fallback are implemented and tested.
- Initial readiness is counted once; subsequent service handling, cutoff equality, capacity, supported cargo, closures and scheduled waiting are enforced. Selection is recomputed against current feasible candidates. ESG totals use remaining legs and correct kg→tonne conversion.
- A forecast becoming null at an abandoned original port after alternate-port selection is not inherently false; an explicit new-route event would improve completeness. `synthetic-model` is valid under the string source schema and displayed literally; no functional failure was demonstrated. Seed forecast replacement alone is not a release blocker.
- An empty audit returns valid with zero checked entries. This can be described more clearly as “nothing to verify”, but it does not establish approval and is distinct from finding 5.
- Auth checks are server-derived bearer roles; mutations require auth and reviewer decisions require reviewer role. Ordinary idempotent replay after review, transactional state/audit writes, revision content conflict and payload/entry tamper detection pass existing tests. Public synthetic GETs, disclosed local demo credentials and no production tenant isolation are accepted scope.
- Synthetic history/weather/schedules, simplified queues, no signed ledger/external trust anchor, no real booking/customs integration, and fixed-clock single-process simulation are clearly scoped limitations, not missing mandatory case-2 functionality.
- Existing request guards prevent old shipment responses from replacing a newly selected shipment in the principal flows. Dirty validation/submission protection is present. This review establishes the separate dirty-review failure above, not a speculative general async-race blocker.

## Recommended release gate

Fix 1–3 and add focused regressions for submitted-form review, revision resubmission and SCT-002 road forecasts. Fix 4–6 before claiming robust API validation, current-state audit integrity and a repeatable demo from arbitrary persisted states. Rerun the 106-test suite/build plus these targeted checks. The mandatory MVP feature set passes; approval of the complete interactive workflow remains conditional on these correctness fixes.
