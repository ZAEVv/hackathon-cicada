#!/usr/bin/env python3
"""Run the asserted, repeatable synthetic SCT-001 workflow against a running API."""
import argparse
import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API origin (default: %(default)s)")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    run_id = uuid.uuid4().hex
    shipment_path = "/api/shipments/SCT-001"
    # Mirror the server: a blank env value means "unset". The built-in defaults
    # are only accepted by the API from a direct loopback connection.
    broker_token = (os.getenv("DEMO_BROKER_TOKEN") or "").strip() or "demo-broker-token"
    reviewer_token = (os.getenv("DEMO_REVIEWER_TOKEN") or "").strip() or "demo-reviewer-token"
    broker = {"Authorization": f"Bearer {broker_token}"}
    reviewer = {"Authorization": f"Bearer {reviewer_token}"}

    def call(path, method="GET", body=None, headers=None):
        # Every /api route except health requires a token; reads use the broker's.
        request_headers = dict(headers or broker)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(f"{base}{path}", data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise RuntimeError(f"Cannot reach API at {base}: {error.reason}") from error

    try:
        # Get back to a known, feasible baseline without changing an existing
        # declaration unless it is currently blocking a fresh submission.
        baseline = call(f"{shipment_path}/scenario/reset", "POST", headers=broker)
        declaration = call(f"{shipment_path}/preclearance")
        if declaration and declaration["status"] == "submitted":
            call(f"{shipment_path}/preclearance/review", "POST", {
                "decision": "needs_revision", "comment": f"Preparing repeatable demo {run_id[:10]}",
            }, reviewer)
        normal = call(shipment_path)
        require(normal["scenario"]["kind"] == "none", "Could not reset scenario to normal")
        require(normal["current_route"]["feasible"], "Normal baseline route is not feasible")

        storm = call(f"{shipment_path}/scenario", "POST", {"kind": "storm"}, broker)
        require(storm["current_route"]["missed_connection"], "Storm did not produce the expected missed connection")
        candidates = [route for route in storm["alternatives"] if route["feasible"]]
        require(bool(candidates), "Storm scenario has no feasible recovery alternatives")
        chosen = candidates[0]
        selected = call(f"{shipment_path}/route", "POST", {"route_id": chosen["id"]}, broker)
        require(selected["selected_route_id"] == chosen["id"], "Route selection was not applied")

        # Submitted is the sole state that cannot be edited. Move it to revision;
        # ready/accepted/draft/absent declarations can be saved under normal rules.
        existing = call(f"{shipment_path}/preclearance")
        if existing and existing["status"] == "submitted":
            call(f"{shipment_path}/preclearance/review", "POST", {
                "decision": "needs_revision", "comment": f"Preparing repeatable demo {run_id[:10]}",
            }, reviewer)
        payload = {
            "shipper": "Demo Russian Exporter", "consignee": "Demo Iranian Importer",
            "description": "General machine parts", "mass_kg": 20000,
            "value_usd": 45000, "origin_country": "RU", "hs_code": "847990",
            "container_id": "DEMO0000001", "documents": [
                {"kind": "invoice", "number": f"INV-{run_id[:10]}", "container_id": "DEMO0000001", "mass_kg": 20000},
                {"kind": "packing_list", "number": f"PK-{run_id[:10]}", "container_id": "DEMO0000001", "mass_kg": 19000},
                {"kind": "transport", "number": f"TR-{run_id[:10]}", "container_id": "DEMO0000001", "mass_kg": 20000},
            ],
        }
        call(f"{shipment_path}/preclearance", "PUT", payload, broker)
        delayed = call(shipment_path)
        require(delayed["shipment"]["documents_ready_at"] > normal["shipment"]["documents_ready_at"],
                "Invalid documents did not delay forecast readiness")
        failed = call(f"{shipment_path}/preclearance/validate", "POST", headers=broker)
        require(failed["status"] == "validation_failed", "Wrong-mass documents were not rejected")
        require(any(issue["code"] == "mass_mismatch" for issue in failed["validation"]["issues"]),
                "Validation did not identify the document mass mismatch")

        payload["documents"][1]["mass_kg"] = 20000
        call(f"{shipment_path}/preclearance", "PUT", payload, broker)
        ready = call(f"{shipment_path}/preclearance/validate", "POST", headers=broker)
        require(ready["status"] == "ready", "Corrected declaration did not validate")
        fixed_forecast = call(shipment_path)
        require(fixed_forecast["shipment"]["documents_ready_at"] == normal["shipment"]["documents_ready_at"],
                "Valid documents did not restore baseline readiness")

        audit_path = f"{shipment_path}/audit"
        audit_before_submit = call(audit_path)
        submit_headers = {**broker, "Idempotency-Key": f"demo-{run_id}"}
        submitted = call(f"{shipment_path}/preclearance/submit", "POST", headers=submit_headers)
        require(submitted["status"] == "submitted", "Declaration submit failed")
        after_submit = call(audit_path)
        require(len(after_submit) == len(audit_before_submit) + 1, "Submit did not append exactly one audit event")
        replay = call(f"{shipment_path}/preclearance/submit", "POST", headers=submit_headers)
        require(replay == submitted, "Idempotent retry did not return the original submission")
        require(len(call(audit_path)) == len(after_submit), "Idempotent retry appended another audit event")
        accepted = call(f"{shipment_path}/preclearance/review", "POST", {
            "decision": "demo_accepted", "comment": f"Demo run {run_id[:10]} reviewed",
        }, reviewer)
        require(accepted["status"] == "demo_accepted", "Reviewer did not accept the demo declaration")
        reviewed_audit = call(audit_path)
        require(len(reviewed_audit) == len(after_submit) + 1, "Review did not append exactly one audit event")
        replay_after_review = call(f"{shipment_path}/preclearance/submit", "POST", headers=submit_headers)
        require(replay_after_review == submitted, "Retry after review did not return original submit response")
        require(len(call(audit_path)) == len(reviewed_audit), "Post-review retry appended an audit event")
        verification = call(f"{shipment_path}/audit/verify")
        require(verification["valid"], "Audit hash chain verification failed")

        final_normal = call(f"{shipment_path}/scenario/reset", "POST", headers=broker)
        require(final_normal["scenario"]["kind"] == "none", "Could not restore normal scenario")
        print(json.dumps({
            "run_id": run_id,
            "normal": {"risk": normal["prediction"]["risk"], "route_feasible": normal["current_route"]["feasible"]},
            "storm": {"missed_connection": storm["current_route"]["missed_connection"], "feasible_alternatives": len(candidates)},
            "route_selection": {"route_id": selected["selected_route_id"], "retained_until_reset": True},
            "documents": {"invalid_status": failed["status"], "mass_mismatch_reported": True, "fixed_status": ready["status"], "readiness_recovered": True},
            "submission": {"status": submitted["status"], "replay_stable": True, "reviewed_status": accepted["status"]},
            "audit": {"entries": len(reviewed_audit), "valid": verification["valid"], "replay_added_no_entries": True},
            "final_scenario": final_normal["scenario"]["kind"],
        }, indent=2))
    except (RuntimeError, KeyError, IndexError) as error:
        try:
            call(f"{shipment_path}/scenario/reset", "POST", headers=broker)
        except RuntimeError:
            pass
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
