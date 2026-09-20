"""End-to-end tests for the /weight-summary endpoint."""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from app.summary import KG_TO_LB

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit


def summary(scans, target_unit=None):
    body = {"scans": scans}
    if target_unit is not None:
        body["target_unit"] = target_unit
    return client.post("/weight-summary", json=body)


def scan(gtin=GTIN_OK, batch=None, weight="3103002513", extra=""):
    """Build an element-string scan carrying exactly the required AIs."""
    parts = [f"01{gtin}"]
    if extra:
        parts.append(extra)
    if batch is not None:
        parts.append(f"10{batch}{GS}")
    parts.append(weight)
    return "".join(parts)


# ---------------------------------------------------------------- happy path


def test_grouping_by_gtin_and_batch_in_first_seen_order():
    resp = summary(
        [
            scan(batch="A"),
            scan(batch="B", weight="3103002000"),
            scan(batch="A"),  # interleaved: must not move group A
            scan(gtin="00000000000017", batch="A"),  # different GTIN -> new group
        ]
    )
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert [(g["gtin"], g["batch"]) for g in groups] == [
        (GTIN_OK, "A"),
        (GTIN_OK, "B"),
        ("00000000000017", "A"),
    ]
    assert [g["count"] for g in groups] == [2, 1, 1]
    assert all(g["unit"] == "kg" for g in groups)
    assert groups[0]["total"] == 2 * 2.513


def test_default_target_unit_is_kg():
    resp = summary([scan()])
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "kg"
    assert body["groups"][0]["unit"] == "kg"


def test_without_batch_groups_under_null_key():
    resp = summary([scan(), scan(weight="3103002000")])
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["batch"] is None
    assert groups[0]["count"] == 2
    assert groups[0]["total"] == 4.513


def test_present_and_absent_batch_are_distinct_groups():
    resp = summary([scan(batch="A"), scan()])
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert [g["batch"] for g in groups] == ["A", None]


def test_lb_scan_converted_to_kg():
    # 25.13 lb * 0.45359237 = 11.3987762581 kg
    resp = summary([scan(weight="3202002513")])
    assert resp.status_code == 200
    group = resp.json()["groups"][0]
    assert group["unit"] == "kg"
    assert group["total"] == 11.398776


def test_kg_scan_converted_to_lb():
    # 1 kg / 0.45359237 = 2.2046226218... lb
    resp = summary([scan(weight="3100000001")], target_unit="lb")
    assert resp.status_code == 200
    group = resp.json()["groups"][0]
    assert group["unit"] == "lb"
    assert group["total"] == 2.204623


def test_mixed_units_inside_one_group():
    # 2.513 kg + 25.13 lb = 2.513 + 11.3987762581 = 13.9117762581 kg
    resp = summary([scan(batch="B1"), scan(batch="B1", weight="3202002513")])
    assert resp.status_code == 200
    group = resp.json()["groups"][0]
    assert group["count"] == 2
    assert group["total"] == 13.911776


def test_same_unit_scans_are_not_converted():
    resp = summary([scan(weight="3202002513")] * 2, target_unit="lb")
    assert resp.status_code == 200
    group = resp.json()["groups"][0]
    assert group["total"] == 50.26
    assert group["unit"] == "lb"


def test_ai_17_and_21_are_allowed_but_ignored():
    resp = summary(
        [f"01{GTIN_OK}1725013110B1{GS}21SN0001{GS}3103002513"]
    )
    assert resp.status_code == 200
    group = resp.json()["groups"][0]
    assert group["gtin"] == GTIN_OK
    assert group["batch"] == "B1"
    assert group["total"] == 2.513
    assert set(group) == {"gtin", "batch", "count", "unit", "total"}


def test_digital_link_scans_accepted():
    resp = summary(
        ["https://id.gs1.org/01/09501101530003/3103/002513?10=B1"]
    )
    assert resp.status_code == 200
    group = resp.json()["groups"][0]
    assert group["batch"] == "B1"
    assert group["total"] == 2.513


def test_empty_scan_list():
    resp = summary([])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "groups": []}


def test_empty_scan_list_lb_target():
    resp = summary([], target_unit="lb")
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "lb", "groups": []}


# ------------------------------------------------- exact Decimal reconstruction


def test_weight_rebuilt_from_raw_digits_and_precision_exactly():
    # 3109 000001 -> int(1) / 10**9 = 0.000000001 kg; three scans sum
    # exactly with Decimal; float accumulation of 1e-9 would drift.
    from app.summary import summarize

    result = summarize(["01095011015300033109000001"] * 3, "kg")
    total = result["groups"][0]["total"]
    assert total == Decimal("0.000000003")
    assert not isinstance(total, float)


def test_mixed_unit_total_is_exact_decimal_before_rounding():
    from app.summary import summarize

    result = summarize(
        [scan(batch="B1"), scan(batch="B1", weight="3202002513")], "kg"
    )
    assert result["groups"][0]["total"] == Decimal("13.9117762581")


def test_kg_to_lb_uses_exact_reciprocal_not_float():
    from app.summary import summarize

    result = summarize([scan(weight="3100000001")], "lb")
    assert result["groups"][0]["total"] == KG_TO_LB


def test_totals_rendered_as_six_place_json_numbers():
    resp = summary([scan(weight="3103002513")])
    # compact JSON; fixed six decimals must appear in the raw body and the
    # token must be a JSON number, not a quoted string.
    assert '"total":2.513000' in resp.text
    assert '"total":"2.513000"' not in resp.text


def test_integer_total_keeps_six_decimals():
    resp = summary([scan(weight="3100000001")])
    assert '"total":1.000000' in resp.text
    assert resp.json()["groups"][0]["total"] == 1.0


def test_high_precision_value_rounds_to_zero_at_six_places():
    resp = summary(["01095011015300033109000001"])
    assert '"total":0.000000' in resp.text


def test_half_even_rounding_at_six_places():
    # 0.0000005 -> nearest even: 0.000000
    resp = summary(["01095011015300033107000005"])
    assert '"total":0.000000' in resp.text
    # 0.0000015 -> half-even rounds up to 0.000002
    resp = summary(["01095011015300033107000015"])
    assert '"total":0.000002' in resp.text
    # 0.0000025 -> half-even rounds down to 0.000002
    resp = summary(["01095011015300033107000025"])
    assert '"total":0.000002' in resp.text


def test_rounding_happens_after_summing_not_per_scan():
    # Each scan is 0.0000004 kg -> 0.000000 at six places individually,
    # but two scans sum to 0.0000008 -> 0.000001.
    resp = summary(["01095011015300033107000004"] * 2)
    assert '"total":0.000001' in resp.text


# ---------------------------------------------------------------- errors


def test_parser_error_reports_scan_index_and_original_error():
    resp = summary([scan(), "0109501101530004"])  # bad check digit at index 1
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"
    assert detail["error"]["offset"] == 15
    assert detail["error"]["found"] == "4"
    assert "Mod-10" in detail["error"]["expected"]


def test_empty_string_scan_fails_with_index():
    resp = summary(["01095011015300033103002513", ""])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["error"]["error"] == "EMPTY_INPUT"


def test_missing_gtin():
    resp = summary(["3103002513"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "01" in detail["error"]["message"]
    assert "310n/320n" in detail["error"]["expected"]


def test_missing_weight():
    resp = summary([f"01{GTIN_OK}10B1"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "310n/320n" in detail["error"]["message"]


def test_duplicate_gtin_rejected():
    resp = summary([f"01{GTIN_OK}01{GTIN_OK}3103002513"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert detail["error"]["offset"] == 16  # offset of the second AI 01
    assert "01" in detail["error"]["message"]


def test_duplicate_batch_rejected():
    resp = summary([f"01{GTIN_OK}10A{GS}10B{GS}3103002513"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert detail["error"]["offset"] == 20  # offset of the second AI 10


def test_duplicate_weight_same_family_rejected():
    # 3103 then 3101: two weight elements, self-delimiting fixed fields.
    resp = summary([f"01{GTIN_OK}31030025133101012345"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103" in detail["error"]["found"]
    assert "3101" in detail["error"]["found"]


def test_kg_and_lb_weight_together_rejected():
    resp = summary([f"01{GTIN_OK}31030025133202002513"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert detail["error"]["offset"] == 26


def test_digital_link_duplicate_ai_surfaces_as_parser_error():
    resp = summary(["https://id.gs1.org/3103/002513?3103=000001"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"


def test_first_failing_scan_index_reported():
    resp = summary(
        [
            scan(),
            scan(weight="3103002000"),
            "99ABC",  # index 2: UNSUPPORTED_AI
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 2
    assert detail["error"]["error"] == "UNSUPPORTED_AI"


def test_error_detail_shape_matches_parse_endpoint_plus_index():
    # Month 13 in AI 17: the original parser error is forwarded untouched
    # and only tagged with the scan index.
    resp = summary([f"01{GTIN_OK}172513313103002513"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert set(detail) == {"scan_index", "error"}
    assert detail["error"]["error"] == "INVALID_DATE"
    assert {"error", "message", "offset", "expected"} <= set(detail["error"])


# ---------------------------------------------------- request model validation


def test_missing_scans_field_is_422():
    resp = client.post("/weight-summary", json={})
    assert resp.status_code == 422


def test_unknown_target_unit_is_422():
    resp = summary([scan()], target_unit="stone")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "target_unit"]


def test_non_string_scan_entry_is_422():
    resp = client.post("/weight-summary", json={"scans": [123]})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "scans", 0]


def test_scans_not_a_list_is_422():
    resp = client.post("/weight-summary", json={"scans": "01095011015300033103002513"})
    assert resp.status_code == 422
