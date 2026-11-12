#!/usr/bin/env python3
"""Asserted, repeatable walkthrough of the final jury scenario against a running API.

Covers: six shipments (western line + eastern branch), storm -> Makhachkala
bottleneck in the 72 h hub forecast, closure -> Amirabad alternative, route
choice reflected everywhere, scenario reset, pre-clearance automatic path
(trusted shipper -> auto_accepted) and manual path (untrusted shipper ->
reviewer needs_revision -> new draft -> demo_accepted), idempotent replay,
audit chain verification, corridor summary and model metrics.

Assertions are relations ("storm peak > base peak", "an alternative via
Amirabad exists"), not exact numbers, so demo-balance retuning does not break
them. Prints one PASS/FAIL line per check; exits non-zero if anything failed.

Usage: .venv/bin/python scripts/demo.py [--base-url URL]
       (or SCT_BASE_URL=http://127.0.0.1:5173 for the Docker container)
"""
import argparse
import json
import math
import os
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
SHIPMENTS = [f"SCT-00{i}" for i in range(1, 7)]
WEST, EAST = "SCT-001", "SCT-004"
TRUSTED_SHIPPER = "Demo Russian Exporter"
UNTRUSTED_SHIPPER = "Demo Unlisted Trading Co"


class ApiError(RuntimeError):
    def __init__(self, method, path, status, detail):
        super().__init__(f"{method} {path} returned HTTP {status}: {detail[:300]}")
        self.status = status


class Walkthrough:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.run_id = uuid.uuid4().hex[:10]
        # Mirror the server: a blank env value means "unset". The built-in
        # defaults are only accepted by the API from a direct loopback peer.
        broker = (os.getenv("DEMO_BROKER_TOKEN") or "").strip() or "demo-broker-token"
        reviewer = (os.getenv("DEMO_REVIEWER_TOKEN") or "").strip() or "demo-reviewer-token"
        self.broker = {"Authorization": f"Bearer {broker}"}
        self.reviewer = {"Authorization": f"Bearer {reviewer}"}
        self.passed = 0
        self.failed = []
        self.facts = {}
        self.now = ""

    # ------------------------------------------------------------ plumbing
    def call(self, path, method="GET", body=None, headers=None, expect=200):
        request_headers = dict(headers or self.broker)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(f"{self.base}{path}", data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except HTTPError as error:
            status, raw = error.code, error.read()
        except URLError as error:
            raise RuntimeError(f"Cannot reach API at {self.base}: {error.reason}") from error
        if status != expect:
            raise ApiError(method, path, status, raw.decode(errors="replace"))
        return json.loads(raw) if raw else None

    def check(self, condition, message):
        if condition:
            self.passed += 1
            print(f"  PASS  {message}")
        else:
            self.failed.append(message)
            print(f"  FAIL  {message}")
        return bool(condition)

    def step(self, title, function):
        print(f"\n== {title}")
        try:
            function()
        except (RuntimeError, KeyError, IndexError, TypeError, ValueError, StopIteration) as error:
            self.failed.append(f"{title}: {type(error).__name__}: {error}")
            print(f"  FAIL  {title} aborted: {type(error).__name__}: {error}")

    def shipment(self, sid):
        return self.call(f"/api/shipments/{sid}")

    def scenario(self, sid, kind):
        return self.call(f"/api/shipments/{sid}/scenario", "POST", {"kind": kind})

    def reset(self, sid):
        return self.call(f"/api/shipments/{sid}/scenario/reset", "POST")

    def forecast(self, sid):
        return self.call(f"/api/hubs/forecast?shipment_id={sid}")

    def verify(self, sid, when):
        result = self.call(f"/api/shipments/{sid}/audit/verify")
        self.check(result["valid"], f"{sid} audit chain valid {when} ({result['entries_checked']} entries)")

    def audit_len(self, sid):
        return len(self.call(f"/api/shipments/{sid}/audit"))

    def unblock(self, sid):
        """A leftover 'submitted' declaration (e.g. an aborted run) cannot be edited."""
        existing = self.call(f"/api/shipments/{sid}/preclearance")
        if existing and existing["status"] == "submitted":
            self.call(f"/api/shipments/{sid}/preclearance/review", "POST", {
                "decision": "needs_revision", "comment": f"Repeatable demo {self.run_id}"}, self.reviewer)

    @staticmethod
    def hub(forecast, port_id):
        return next(h for h in forecast["hubs"] if h["port_id"] == port_id)

    def well_formed(self, forecast, label):
        hubs = forecast["hubs"]
        ok = bool(hubs) and all(
            len(h["points"]) == forecast["horizon_hours"] // forecast["step_hours"] + 1
            and all(math.isfinite(p["utilization"]) and math.isfinite(p["queue_hours"]) for p in h["points"])
            for h in hubs)
        keys = [(w["start_at"], -w["peak_utilization"], w["port_id"]) for w in forecast["bottlenecks"]]
        self.check(ok and forecast["horizon_hours"] == 72 and keys == sorted(keys),
                   f"{label}: 72 h hourly series for {len(hubs)} hubs, finite, windows sorted")

    def payload(self, sid, shipper, packing_mass_delta=0):
        item = self.shipment(sid)["shipment"]
        mass, container = item["mass_kg"], item["container_id"]
        tag = f"{self.run_id}-{sid[-3:]}"
        return {
            "shipper": shipper, "consignee": "Demo Iranian Importer",
            "description": "General machine parts", "mass_kg": mass, "value_usd": 45000,
            "origin_country": "RU", "hs_code": "847990", "container_id": container,
            "documents": [
                {"kind": "invoice", "number": f"INV-{tag}", "container_id": container, "mass_kg": mass},
                {"kind": "packing_list", "number": f"PK-{tag}", "container_id": container,
                 "mass_kg": mass + packing_mass_delta},
                {"kind": "transport", "number": f"TR-{tag}", "container_id": container, "mass_kg": mass},
            ],
        }

    def save_and_validate(self, sid, payload, expected_status):
        path = f"/api/shipments/{sid}"
        saved = self.call(f"{path}/preclearance", "PUT", payload)
        self.check(saved["status"] == "draft", f"{sid} draft v{saved['version']} saved")
        validated = self.call(f"{path}/preclearance/validate", "POST")
        self.check(validated["status"] == expected_status, f"{sid} validate -> {validated['status']}")
        return validated

    # ------------------------------------------------------------ steps
    def step_start(self):
        health = self.call("/api/health", headers={})
        self.now = health["simulation_now"]
        self.check(health["status"] == "ok" and health["demo_only"], "API healthy, demo-only flag set")
        for sid in SHIPMENTS:
            self.reset(sid)
            self.unblock(sid)
        for sid in (WEST, EAST):
            # A pending draft from an aborted run would delay readiness and skew
            # the baseline: bring it back to a valid, ready declaration first.
            existing = self.call(f"/api/shipments/{sid}/preclearance")
            if existing and existing["status"] in ("draft", "validation_failed", "needs_revision"):
                self.call(f"/api/shipments/{sid}/preclearance", "PUT", self.payload(sid, UNTRUSTED_SHIPPER))
                self.call(f"/api/shipments/{sid}/preclearance/validate", "POST")
        dashboard = self.call("/api/dashboard")
        ids = sorted(d["shipment"]["id"] for d in dashboard["shipments"])
        self.check(set(SHIPMENTS) <= set(ids), f"dashboard lists six demo shipments ({', '.join(ids)})")
        self.check(dashboard["summary"]["total"] == len(ids), "dashboard total matches shipment list")
        by_id = {d["shipment"]["id"]: d for d in dashboard["shipments"]}
        locations = {by_id[sid]["shipment"]["current_location_id"] for sid in SHIPMENTS}
        self.check({"makhachkala", "aktau"} <= locations, "western (Makhachkala) and eastern (Aktau) branches present")
        catalog = self.call("/api/catalog")
        ports = {p["id"] for p in catalog["ports"]}
        self.check({"aktau", "turkmenbashi", "amirabad"} <= ports, "catalog has Aktau, Turkmenbashi, Amirabad")
        self.facts["baseline"] = {}
        for sid in SHIPMENTS:
            detail = self.shipment(sid)
            self.facts["baseline"][sid] = detail
            self.check(detail == by_id[sid], f"{sid} detail equals its dashboard entry")
            self.check(detail["scenario"]["kind"] == "none" and detail["selected_route_id"] is None,
                       f"{sid} starts on base scenario without a chosen route")
            routes = [detail["current_route"], *detail["alternatives"]]
            self.check(bool(detail["current_route"]["legs"]) or bool(detail["current_route"]["reason"]),
                       f"{sid} current route has legs or a stated reason")
            self.check(all(r["eta"] > health["simulation_now"] for r in routes if r["eta"]),
                       f"{sid} every ETA is after the simulation clock")
        west = self.facts["baseline"][WEST]
        self.check(west["current_route"]["feasible"], f"{WEST} planned route feasible in the base scenario")
        self.facts["west_risk"] = west["prediction"]["risk"]

    def step_storm(self):
        base = self.forecast(WEST)
        self.well_formed(base, "base hub forecast")
        storm = self.scenario(WEST, "storm")
        self.check(storm["current_route"]["missed_connection"], f"{WEST} storm: planned connection missed")
        self.check(storm["prediction"]["miss_probability"]
                   >= self.facts["baseline"][WEST]["prediction"]["miss_probability"],
                   f"{WEST} storm: miss probability not lower than base")
        self.check(storm["scenario"].get("wave_height_m") is not None
                   and "High sea state" in storm["prediction"]["factors"],
                   f"{WEST} storm: wave height {storm['scenario'].get('wave_height_m')} m in scenario and "
                   f"'High sea state' factor")
        self.check(storm["weather"].get("wave_height_m") is not None, f"{WEST} weather carries wave_height_m")
        self.check(any(r["feasible"] for r in storm["alternatives"]), f"{WEST} storm: feasible recovery exists")
        hit = self.forecast(WEST)
        self.well_formed(hit, "storm hub forecast")
        base_peak = self.hub(base, "makhachkala")["peak_utilization"]
        storm_peak = self.hub(hit, "makhachkala")["peak_utilization"]
        self.check(storm_peak > base_peak, f"Makhachkala storm peak {storm_peak:.0%} > base peak {base_peak:.0%}")
        windows = [w for w in hit["bottlenecks"] if w["port_id"] == "makhachkala"
                   and "Storm limits berth operations" in w["reasons"]]
        self.check(bool(windows), "storm bottleneck window at Makhachkala with storm reason")
        self.check(any(WEST in w["shipment_ids"] for w in windows), f"{WEST} is inside the Makhachkala storm window")
        if windows:
            first = windows[0]
            self.facts["storm_window"] = f"{first['start_at']}..{first['end_at']} peak {first['peak_utilization']:.0%}"
        self.facts["storm"] = {"missed_connection": storm["current_route"]["missed_connection"],
                               "risk": storm["prediction"]["risk"]}

    def step_closure(self):
        closure = self.scenario(WEST, "closure")
        current = closure["current_route"]
        self.check(not current["feasible"] and current["reason"], f"{WEST} closure: planned route unavailable with reason")
        feasible = [r for r in closure["alternatives"] if r["feasible"]]
        self.check(all(leg["service_id"] != "sea-main" for r in feasible for leg in r["legs"]),
                   "no feasible alternative uses the closed sea-main")
        amirabad = [r for r in feasible if any(leg["destination_id"] == "amirabad" for leg in r["legs"])]
        self.check(bool(amirabad), "a feasible alternative via Amirabad exists")
        dashboard = self.call("/api/dashboard")
        summary = self.call("/api/summary?weeks=4")
        status = next(r["status"] for r in summary["recent_shipments"] if r["id"] == WEST)
        self.check(status == "at_risk", f"summary marks {WEST} at risk while its route is unavailable")
        at_risk_closed = dashboard["summary"]["high_risk"]
        choice = amirabad[0]
        chosen = self.call(f"/api/shipments/{WEST}/route", "POST", {"route_id": choice["id"]})
        self.check(chosen["selected_route_id"] == choice["id"] and chosen["current_route"]["id"] == choice["id"]
                   and chosen["current_route"]["feasible"], f"{WEST} Amirabad route chosen and now current")
        self.check(all(r["id"] != choice["id"] for r in chosen["alternatives"]), "chosen route left the alternatives")
        delivery = next((e for e in chosen["shipment"]["events"] if e["event_type"] == "delivery"), None)
        self.check(delivery is None or delivery["estimated_at"] == chosen["current_route"]["eta"],
                   "delivery event estimate follows the chosen ETA")
        self.check(self.shipment(WEST) == chosen, "shipment detail reflects the choice")
        dashboard = self.call("/api/dashboard")
        entry = next(d for d in dashboard["shipments"] if d["shipment"]["id"] == WEST)
        self.check(entry == chosen, "dashboard reflects the choice")
        self.check(dashboard["summary"]["high_risk"] < at_risk_closed, "dashboard at-risk count drops after the choice")
        forecast = self.forecast(WEST)
        horizon_end = forecast["hubs"][0]["points"][-1]["at"]
        seen = {h["port_id"] for h in forecast["hubs"] for p in h["points"] if WEST in p["shipment_ids"]}
        nodes = {leg["origin_id"] for leg in choice["legs"]} | {leg["destination_id"] for leg in choice["legs"]}
        reaches = any(leg["destination_id"] == "amirabad" and leg["arrival_at"] <= horizon_end for leg in choice["legs"])
        self.check(seen and seen <= nodes and ("amirabad" in seen or not reaches),
                   f"hub forecast follows {WEST} along the chosen route ({', '.join(sorted(seen))})")
        self.check(not any(WEST in w["shipment_ids"] and "Shipment held: route unavailable" in w["reasons"]
                           for w in forecast["bottlenecks"]), f"{WEST} no longer held at its node")
        self.facts["closure"] = {"chosen_route": choice["id"], "eta": choice["eta"],
                                 "co2_kg": round(choice["co2_kg"])}

    def step_reset(self):
        reset = self.reset(WEST)
        base = self.facts["baseline"][WEST]
        self.check(reset["scenario"]["kind"] == "none" and reset["selected_route_id"] is None, f"{WEST} reset to base")
        self.check(reset["current_route"] == base["current_route"] and reset["prediction"] == base["prediction"],
                   f"{WEST} reset restores the base route and forecast")
        # Every scenario on every shipment answers with a usable picture.
        for kind in ("storm", "congestion", "closure"):
            problems = []
            for sid in SHIPMENTS:
                detail = self.scenario(sid, kind)
                current, alternatives = detail["current_route"], detail["alternatives"]
                if not (current["legs"] or current["reason"]):
                    problems.append(f"{sid}: empty current route without a reason")
                if current["feasible"] == bool(current["reason"]):
                    problems.append(f"{sid}: current route feasible flag contradicts its reason")
                if not all(r["feasible"] and r["legs"] and not r["missed_connection"] for r in alternatives):
                    problems.append(f"{sid}: an alternative is not a feasible, non-missed route")
                if not any(r["feasible"] for r in [current, *alternatives]):
                    problems.append(f"{sid}: no feasible option")
                if any(r["eta"] and r["eta"] <= self.now
                       for r in [current, *alternatives]):
                    problems.append(f"{sid}: ETA not after the simulation clock")
            self.check(not problems, f"{kind}: all six shipments have a consistent route picture"
                       + (f" ({'; '.join(problems)})" if problems else ""))
            for sid in SHIPMENTS:
                self.reset(sid)

    def step_preclearance_auto(self):
        sid, path = WEST, f"/api/shipments/{WEST}"
        base_ready = self.facts["baseline"][sid]["shipment"]["documents_ready_at"]
        self.unblock(sid)
        ruleset = self.call("/api/preclearance/ruleset")
        self.check(ruleset["id"] == "demo-rules-v1" and len(ruleset["rules"]) >= 1,
                   f"ruleset {ruleset['id']} with {len(ruleset['rules'])} rules")
        failed = self.save_and_validate(sid, self.payload(sid, TRUSTED_SHIPPER, -1000), "validation_failed")
        self.check(any(i["code"] == "mass_mismatch" for i in failed["validation"]["issues"]),
                   "packing list 1 t off -> mass_mismatch")
        delayed = self.shipment(sid)
        self.check(delayed["shipment"]["documents_ready_at"] > base_ready, "invalid documents delay readiness")
        self.verify(sid, "after failed validation")
        self.save_and_validate(sid, self.payload(sid, TRUSTED_SHIPPER), "ready")
        self.check(self.shipment(sid)["shipment"]["documents_ready_at"] == base_ready,
                   "ready documents restore baseline readiness")
        self.verify(sid, "after ready")
        before = self.audit_len(sid)
        key = {**self.broker, "Idempotency-Key": f"demo-auto-{self.run_id}"}
        submitted = self.call(f"{path}/preclearance/submit", "POST", headers=key)
        clearance = submitted.get("clearance") or {}
        self.check(submitted["status"] == "auto_accepted" and clearance.get("decision") == "auto_accepted"
                   and all(r["passed"] for r in clearance.get("results", [])),
                   f"trusted shipper -> {submitted['status']} by {clearance.get('ruleset_id')}")
        self.check(self.audit_len(sid) == before + 1, "submit appended exactly one audit entry")
        replay = self.call(f"{path}/preclearance/submit", "POST", headers=key)
        self.check(replay == submitted and self.audit_len(sid) == before + 1,
                   "idempotent replay returns the same answer without a new audit entry")
        after = self.shipment(sid)
        self.check(after["shipment"]["documents_ready_at"] == base_ready
                   and after["prediction"] == self.facts["baseline"][sid]["prediction"],
                   "auto_accepted does not delay the cargo")
        self.check(after["preclearance"]["status"] == "auto_accepted", "shipment detail shows auto_accepted")
        try:
            self.call(f"{path}/preclearance/review", "POST", {"decision": "demo_accepted", "comment": "n/a"},
                      self.reviewer, expect=409)
            self.check(True, "auto_accepted declaration is not open for manual review (409)")
        except ApiError as error:
            self.check(False, f"review of auto_accepted: {error}")
        self.verify(sid, "after auto acceptance")
        self.facts["auto"] = {"shipment": sid, "status": submitted["status"]}

    def step_preclearance_manual(self):
        sid, path = EAST, f"/api/shipments/{EAST}"
        self.unblock(sid)
        self.save_and_validate(sid, self.payload(sid, UNTRUSTED_SHIPPER), "ready")
        key = {**self.broker, "Idempotency-Key": f"demo-manual-{self.run_id}"}
        submitted = self.call(f"{path}/preclearance/submit", "POST", headers=key)
        failed_rules = [r["rule_id"] for r in (submitted.get("clearance") or {}).get("results", []) if not r["passed"]]
        self.check(submitted["status"] == "submitted" and failed_rules,
                   f"untrusted shipper -> submitted for manual review (failed: {', '.join(failed_rules)})")
        self.verify(sid, "after manual submit")
        try:
            self.call(f"{path}/preclearance/review", "POST", {"decision": "demo_accepted", "comment": "self"},
                      expect=403)
            self.check(True, "broker cannot decide its own declaration (403)")
        except ApiError as error:
            self.check(False, f"broker review: {error}")
        revision = self.call(f"{path}/preclearance/review", "POST", {
            "decision": "needs_revision", "comment": f"Demo {self.run_id}: confirm consignee"}, self.reviewer)
        self.check(revision["status"] == "needs_revision", "reviewer -> needs_revision")
        self.verify(sid, "after needs_revision")
        replay = self.call(f"{path}/preclearance/submit", "POST", headers=key)
        self.check(replay == submitted, "replay after review returns the original submit answer")
        payload = self.payload(sid, UNTRUSTED_SHIPPER)
        payload["consignee"] = "Demo Iranian Importer (confirmed)"
        validated = self.save_and_validate(sid, payload, "ready")
        self.check(validated["version"] > submitted["version"], "revision saved as a new version")
        key2 = {**self.broker, "Idempotency-Key": f"demo-manual2-{self.run_id}"}
        resubmitted = self.call(f"{path}/preclearance/submit", "POST", headers=key2)
        self.check(resubmitted["status"] == "submitted", "revised declaration resubmitted")
        accepted = self.call(f"{path}/preclearance/review", "POST", {
            "decision": "demo_accepted", "comment": f"Demo {self.run_id} reviewed"}, self.reviewer)
        self.check(accepted["status"] == "demo_accepted", "reviewer -> demo_accepted")
        self.verify(sid, "after demo_accepted")
        self.facts["manual"] = {"shipment": sid, "failed_rules": failed_rules, "final": accepted["status"]}

    def step_summary(self):
        results = {}
        for weeks in (4, 13):
            summary = self.call(f"/api/summary?weeks={weeks}")
            shares = sum(m["share_pct"] for m in summary["mode_split"])
            recent = {r["id"]: r["status"] for r in summary["recent_shipments"]}
            self.check(summary["weeks"] == weeks and len(summary["weekly"]) == weeks,
                       f"summary {weeks} weeks: {weeks} weekly bins")
            self.check(abs(shares - 100) < 0.5 and set(SHIPMENTS) <= set(recent),
                       f"summary {weeks} weeks: mode split sums to 100 %, six recent shipments")
            results[weeks] = summary["kpis"]["shipments"]["value"]
        self.check(results[13] >= results[4] > 0, f"13-week volume {results[13]:.0f} >= 4-week {results[4]:.0f}")
        statuses = {r["id"]: r["status"] for r in self.call("/api/summary?weeks=4")["recent_shipments"]}
        self.check(statuses[WEST] != "docs_pending" and statuses[EAST] != "docs_pending",
                   "accepted declarations no longer count as pending documents")
        self.facts["summary"] = {"shipments_4w": results[4], "shipments_13w": results[13]}

    def step_model(self):
        metrics = self.call("/api/model/metrics")
        self.check(metrics["model_mae_hours"] < metrics["baseline_mae_hours"],
                   f"{metrics['model_version']}: MAE {metrics['model_mae_hours']:.2f} h < baseline "
                   f"{metrics['baseline_mae_hours']:.2f} h on {metrics['test_rows']} held-out rows")
        self.facts["model"] = metrics["model_version"]

    def run(self):
        print(f"Smart Caspian Transit demo walkthrough against {self.base} (run {self.run_id})")
        self.step("Start: six shipments on the base scenario", self.step_start)
        if "baseline" not in self.facts:
            return self.finish()
        self.step("Storm -> Makhachkala bottleneck in the 72 h hub forecast", self.step_storm)
        self.step("Closure -> Amirabad alternative, choose route", self.step_closure)
        self.step("Reset and scenario matrix", self.step_reset)
        self.step("Pre-clearance: automatic path (demo-rules-v1)", self.step_preclearance_auto)
        self.step("Pre-clearance: manual path (reviewer role)", self.step_preclearance_manual)
        self.step("Corridor summary", self.step_summary)
        self.step("Model metrics", self.step_model)
        return self.finish()

    def finish(self):
        for sid in SHIPMENTS:
            try:
                self.reset(sid)
            except RuntimeError:
                pass
        print("\n" + json.dumps({k: v for k, v in self.facts.items() if k != "baseline"}, indent=2))
        total = self.passed + len(self.failed)
        if self.failed:
            print(f"\nRESULT: FAIL ({len(self.failed)} of {total} checks failed)")
            for message in self.failed:
                print(f"  - {message}")
            return 1
        print(f"\nRESULT: PASS ({total} checks)")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=(os.getenv("SCT_BASE_URL") or "").strip() or DEFAULT_BASE_URL,
                        help="API origin (env SCT_BASE_URL; default %(default)s)")
    args = parser.parse_args()
    try:
        return Walkthrough(args.base_url).run()
    except RuntimeError as error:  # API unreachable
        print(f"RESULT: FAIL ({error})")
        return 2


if __name__ == "__main__":
    sys.exit(main())
