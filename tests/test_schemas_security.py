"""Model-level input hardening for client-supplied request data (no HTTP)."""
import copy
import json
import math

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.app.schemas import (
    ChooseRouteRequest, Declaration, DeclarationInput, Document, IdempotencyKey,
    ReviewRequest, RouteId, ShipmentId,
)

CONTAINER = "CSPU0000013"


def payload(mass=20000):
    return {
        "shipper": "ООО «Волга Экспорт»", "consignee": "Caspian Trade Co.",
        "description": "General machine parts", "mass_kg": mass,
        "value_usd": 45000, "origin_country": "RU", "hs_code": "847990",
        "container_id": CONTAINER,
        "documents": [
            {"kind": kind, "number": f"{kind}-001", "container_id": CONTAINER, "mass_kg": mass}
            for kind in ("invoice", "packing_list", "transport")
        ],
    }


def declaration_with(**fields):
    data = payload()
    data.update(fields)
    return data


def document_with(**fields):
    data = payload()
    data["documents"][0].update(fields)
    return data


def rejects(model, data, error_type=None):
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    if error_type:
        accepted = {error_type} if isinstance(error_type, str) else set(error_type)
        assert accepted & {e["type"] for e in exc.value.errors()}, exc.value.errors()
    return exc.value


# pydantic-core itself rejects lone surrogates ("string_unicode") before our validator
# runs; either outcome is a clean ValidationError (422), never a 500.
TEXT_REJECTED = ("forbidden_character", "string_unicode")


# --- 1. Size limits ----------------------------------------------------------

DECLARATION_CAPS = [("shipper", 200), ("consignee", 200), ("description", 2000),
                    ("origin_country", 8), ("hs_code", 32), ("container_id", 32)]
DOCUMENT_CAPS = [("number", 64), ("container_id", 32)]


@pytest.mark.parametrize("field,cap", DECLARATION_CAPS)
def test_declaration_string_caps(field, cap):
    DeclarationInput.model_validate(declaration_with(**{field: "x" * cap}))
    rejects(DeclarationInput, declaration_with(**{field: "x" * (cap + 1)}), "string_too_long")


@pytest.mark.parametrize("field,cap", DOCUMENT_CAPS)
def test_document_string_caps(field, cap):
    DeclarationInput.model_validate(document_with(**{field: "x" * cap}))
    rejects(DeclarationInput, document_with(**{field: "x" * (cap + 1)}), "string_too_long")


def test_caps_count_code_points_not_bytes():
    DeclarationInput.model_validate(declaration_with(shipper="Ж" * 200))
    rejects(DeclarationInput, declaration_with(shipper="Ж" * 201), "string_too_long")


def test_documents_list_cap():
    doc = payload()["documents"][0]
    DeclarationInput.model_validate(declaration_with(documents=[dict(doc) for _ in range(20)]))
    rejects(DeclarationInput, declaration_with(documents=[dict(doc) for _ in range(21)]), "too_long")
    DeclarationInput.model_validate(declaration_with(documents=[]))


def test_review_comment_cap():
    ReviewRequest.model_validate({"decision": "accepted", "comment": "c" * 1000})
    rejects(ReviewRequest, {"decision": "accepted", "comment": "c" * 1001}, "string_too_long")
    assert ReviewRequest.model_validate({"decision": "needs_revision"}).comment == ""


def test_route_id_cap():
    ChooseRouteRequest.model_validate({"route_id": "r" * 128})
    rejects(ChooseRouteRequest, {"route_id": "r" * 129}, "string_too_long")
    rejects(ChooseRouteRequest, {"route_id": ""})


# --- 2. Dangerous characters --------------------------------------------------

FORBIDDEN_EVERYWHERE = {
    "nul": "\x00", "bell": "\x07", "vtab": "\x0b", "formfeed": "\x0c", "cr": "\r",
    "esc": "\x1b", "unit_sep": "\x1f", "del": "\x7f", "c1_first": "\x80", "c1_nel": "\x85",
    "c1_csi": "\x9b", "c1_last": "\x9f", "line_sep": "\u2028", "para_sep": "\u2029",
    "lre": "\u202a", "rle": "\u202b", "pdf": "\u202c", "lro": "\u202d", "rlo": "\u202e",
    "lri": "\u2066", "rli": "\u2067", "fsi": "\u2068", "pdi": "\u2069",
    "lone_high_surrogate": "\ud800", "lone_low_surrogate": "\udfff",
}
SINGLE_LINE_ONLY = {"newline": "\n", "tab": "\t"}
SINGLE_LINE_FIELDS = ["shipper", "consignee", "origin_country", "hs_code", "container_id"]


@pytest.mark.parametrize("char", list(FORBIDDEN_EVERYWHERE.values()) + list(SINGLE_LINE_ONLY.values()),
                         ids=list(FORBIDDEN_EVERYWHERE) + list(SINGLE_LINE_ONLY))
@pytest.mark.parametrize("field", SINGLE_LINE_FIELDS)
def test_single_line_declaration_fields_reject_controls(field, char):
    rejects(DeclarationInput, declaration_with(**{field: f"ab{char}cd"}), TEXT_REJECTED)


@pytest.mark.parametrize("char", list(FORBIDDEN_EVERYWHERE.values()) + list(SINGLE_LINE_ONLY.values()),
                         ids=list(FORBIDDEN_EVERYWHERE) + list(SINGLE_LINE_ONLY))
@pytest.mark.parametrize("field", ["number", "container_id"])
def test_document_fields_reject_controls(field, char):
    rejects(DeclarationInput, document_with(**{field: f"{char}"}), TEXT_REJECTED)


@pytest.mark.parametrize("char", list(FORBIDDEN_EVERYWHERE.values()) + list(SINGLE_LINE_ONLY.values()),
                         ids=list(FORBIDDEN_EVERYWHERE) + list(SINGLE_LINE_ONLY))
def test_route_id_rejects_controls(char):
    rejects(ChooseRouteRequest, {"route_id": f"sea-main{char}"})


@pytest.mark.parametrize("char", FORBIDDEN_EVERYWHERE.values(), ids=FORBIDDEN_EVERYWHERE.keys())
def test_multiline_fields_reject_controls(char):
    rejects(DeclarationInput, declaration_with(description=f"line{char}"), TEXT_REJECTED)
    rejects(ReviewRequest, {"decision": "needs_revision", "comment": f"fix{char}it"}, TEXT_REJECTED)


def test_multiline_fields_allow_newline_and_tab_unchanged():
    text = "Line one\n\tindented\nlast line\n"
    assert DeclarationInput.model_validate(declaration_with(description=text)).description == text
    assert ReviewRequest.model_validate({"decision": "needs_revision", "comment": text}).comment == text


def test_legitimate_unicode_is_accepted_and_not_normalised():
    text = "ООО «Каспий» — Ünïcödé café ﬁ 日本 😀 \u200d"  # incl. ZWJ, ligature, emoji
    model = DeclarationInput.model_validate(declaration_with(shipper=text, description=text))
    assert model.shipper == text and model.description == text
    data = declaration_with(shipper="  padded  ")
    assert DeclarationInput.model_validate(data).model_dump()["shipper"] == "  padded  "


def test_error_message_does_not_echo_forbidden_character():
    error = rejects(DeclarationInput, declaration_with(shipper="a\u202eb"), "forbidden_character")
    message = next(e["msg"] for e in error.errors() if e["type"] == "forbidden_character")
    assert "U+202E" in message and "position 1" in message and "\u202e" not in message


def test_lone_surrogate_from_json_text_is_a_validation_error_not_a_crash():
    raw = json.dumps(declaration_with(shipper="\ud800"))  # -> "\\ud800" escape in JSON text
    assert "\\ud800" in raw
    # pydantic's own JSON parser rejects the lone surrogate at parse time ...
    with pytest.raises(ValidationError):
        DeclarationInput.model_validate_json(raw)
    # ... and the stdlib parser (what FastAPI uses) yields it, which pydantic-core's str
    # validation rejects (our forbidden-character validator is a second line of defence).
    rejects(DeclarationInput, json.loads(raw), TEXT_REJECTED)
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(json.loads('{"decision":"accepted","comment":"\\udc00"}'))
    with pytest.raises(ValidationError):
        ChooseRouteRequest.model_validate(json.loads('{"route_id":"\\ud83d"}'))


# --- 3. Numbers ---------------------------------------------------------------

NON_FINITE = [float("nan"), float("inf"), float("-inf")]
BAD_NUMBERS = NON_FINITE + [0, 0.0, -1, -0.5, True, False, "20000", "1e3", None]


@pytest.mark.parametrize("value", BAD_NUMBERS, ids=repr)
@pytest.mark.parametrize("field", ["mass_kg", "value_usd"])
def test_declaration_numbers_reject(field, value):
    rejects(DeclarationInput, declaration_with(**{field: value}))


@pytest.mark.parametrize("value", BAD_NUMBERS, ids=repr)
def test_document_mass_rejects(value):
    rejects(DeclarationInput, document_with(mass_kg=value))


@pytest.mark.parametrize("model,field,cap", [
    ("declaration", "mass_kg", 1_000_000), ("declaration", "value_usd", 1_000_000_000_000),
    ("document", "mass_kg", 1_000_000)])
def test_number_upper_bounds(model, field, cap):
    build = declaration_with if model == "declaration" else document_with
    DeclarationInput.model_validate(build(**{field: cap}))
    DeclarationInput.model_validate(build(**{field: float(cap)}))
    rejects(DeclarationInput, build(**{field: cap + 1}), "less_than_equal")
    rejects(DeclarationInput, build(**{field: 1e308}), "less_than_equal")


def test_json_non_finite_tokens_rejected():
    for token in ("NaN", "Infinity", "-Infinity", "1e400"):
        raw = json.dumps(payload()).replace('"value_usd": 45000', f'"value_usd": {token}')
        with pytest.raises(ValidationError):
            DeclarationInput.model_validate(json.loads(raw))  # stdlib path (FastAPI)
        with pytest.raises(ValidationError):
            DeclarationInput.model_validate_json(raw)


def test_ints_and_small_floats_accepted_and_dumped_as_floats():
    model = DeclarationInput.model_validate(declaration_with(mass_kg=20000, value_usd=0.01))
    dumped = model.model_dump()
    assert dumped["mass_kg"] == 20000.0 and isinstance(dumped["mass_kg"], float)
    assert isinstance(dumped["documents"][0]["mass_kg"], float)
    assert dumped["value_usd"] == 0.01
    assert all(math.isfinite(v) for v in (dumped["mass_kg"], dumped["value_usd"]))


def test_document_model_directly():
    Document.model_validate({"kind": "invoice", "number": "", "container_id": "", "mass_kg": 1})
    with pytest.raises(ValidationError):
        Document.model_validate({"kind": "invoice", "number": "a\x00", "container_id": "", "mass_kg": 1})


# --- Identifiers ------------------------------------------------------------

SHIPMENT_ID = TypeAdapter(ShipmentId)
IDEMPOTENCY_KEY = TypeAdapter(IdempotencyKey)
ROUTE_ID = TypeAdapter(RouteId)


@pytest.mark.parametrize("value", ["SCT-001", "SCT-002", "SCT-003", "a", "A_b-9", "x" * 64,
                                   "not-a-shipment"])
def test_shipment_id_accepts(value):
    assert SHIPMENT_ID.validate_python(value) == value


@pytest.mark.parametrize("value", ["", "x" * 65, "SCT 001", "SCT-001\n", "../etc", "SCT.001", "a/b",
                                   "SCT-001\x00", "ЅСТ-001", "a%2Fb", "SCT-001\u202e", "\ud800"])
def test_shipment_id_rejects(value):
    with pytest.raises(ValidationError):
        SHIPMENT_ID.validate_python(value)


@pytest.mark.parametrize("value", [
    "bad", "key", "SCT-001-v1", "SCT-001-v12", "SCT-001-vundefined",
    "test-3f2b8c1e-9d4a-4f6b-8e2a-1c3d5e7f9a0b", "3F2B8C1E-9D4A-4F6B-8E2A-1C3D5E7F9A0B",
    "urn:uuid:3f2b8c1e-9d4a-4f6b-8e2a-1c3d5e7f9a0b", "a.b_c~d", "k" * 128])
def test_idempotency_key_accepts(value):
    assert IDEMPOTENCY_KEY.validate_python(value) == value


def test_idempotency_key_accepts_real_uuid4_formats():
    import uuid
    for _ in range(50):
        for value in (str(uuid.uuid4()), f"test-{uuid.uuid4()}", uuid.uuid4().hex):
            IDEMPOTENCY_KEY.validate_python(value)


@pytest.mark.parametrize("value", ["", "k" * 129, " key", "key ", "a b", "key\n", "key\t", "key\x00",
                                   "kéy", "{key}", "a/b", "a+b", "a=b", "a,b", '"key"', "\ud800"])
def test_idempotency_key_rejects(value):
    with pytest.raises(ValidationError):
        IDEMPOTENCY_KEY.validate_python(value)


@pytest.mark.parametrize("value", [
    "rail-main@2026-11-12T20:00:00Z+sea-main@2026-11-13T10:00:00Z+road-final@2026-11-14T08:00:00Z",
    "unavailable", "unavailable:rail-main+sea-main+road-final", "not-a-feasible-route"])
def test_route_id_accepts(value):
    assert ROUTE_ID.validate_python(value) == value
    ChooseRouteRequest.model_validate({"route_id": value})


@pytest.mark.parametrize("value", ["", "r" * 129, "a b", "a/b", "<script>", "a\nb", "a;b"])
def test_route_id_rejects(value):
    with pytest.raises(ValidationError):
        ROUTE_ID.validate_python(value)


def test_route_ids_generated_by_backend_match_route_id_type():
    from backend.app import data, routing
    from backend.app.schemas import SIMULATION_NOW
    catalog = data.load_demo_data()
    checked = 0
    for shipment in catalog["shipments"]:
        for kind in ("none", "storm", "congestion", "closure"):
            for ready_at in (SIMULATION_NOW, "2026-11-13T08:00:00Z", "2026-11-16T08:00:00Z", "2026-12-30T08:00:00Z"):
                prediction = {"ready_at": ready_at}
                routes = routing.compute_routes(copy.deepcopy(shipment), catalog["ports"], catalog["services"],
                                                prediction, routing.scenario_profile(kind), SIMULATION_NOW)
                for route in [routes["current_route"], *routes["alternatives"]]:
                    ROUTE_ID.validate_python(route["id"])
                    checked += 1
    assert checked > 0


# --- Regression: existing legitimate (and deliberately invalid business) data -----

def test_api_flow_payloads_still_validate():
    DeclarationInput.model_validate(payload())
    bad_mass = payload()
    bad_mass["documents"][1]["mass_kg"] = 19000
    DeclarationInput.model_validate(bad_mass)
    # tests/test_integration_review.py: no documents at all
    DeclarationInput.model_validate(declaration_with(documents=[]))
    # tests/test_preclearance.py::test_invalid_documents business-invalid draft
    invalid = payload()
    invalid.update(shipper=" ", origin_country="Russia", hs_code="abc", container_id="C1")
    invalid["documents"][0].update(number="", mass_kg=19998, container_id="wrong")
    invalid["documents"][1] = copy.deepcopy(invalid["documents"][0])
    DeclarationInput.model_validate(invalid)
    # Frontend initial form: blank strings everywhere (value_usd 0 already failed gt=0 before).
    blank = declaration_with(shipper="", consignee="", description="", origin_country="",
                             hs_code="", container_id="")
    for doc in blank["documents"]:
        doc.update(number="", container_id="")
    DeclarationInput.model_validate(blank)
    ReviewRequest.model_validate({"decision": "accepted", "comment": "Checked"})
    ReviewRequest.model_validate({"decision": "accepted", "comment": ""})


def test_payload_dump_round_trip_is_byte_identical_for_hashing():
    data = declaration_with(description="Line\n\tTwo", shipper="ООО «Каспий»")
    once = DeclarationInput.model_validate(data).model_dump()
    twice = DeclarationInput.model_validate(once).model_dump()
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)
    assert once["description"] == data["description"]


def test_response_model_tolerates_rows_stored_before_hardening():
    legacy = payload()
    legacy.update(shipper="x" * 5000, description="bell\x07", value_usd=float("inf"))
    legacy["documents"] = [dict(legacy["documents"][0]) for _ in range(50)]
    declaration = {"id": "declaration-SCT-001", "shipment_id": "SCT-001", "version": 1,
                   "status": "draft", "payload": legacy,
                   "validation": {"valid": False, "issues": []}, "content_hash": "0" * 64}
    Declaration.model_validate(declaration)
