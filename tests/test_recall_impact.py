"""End-to-end tests for the /recall-impact endpoint."""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit
GTIN2 = "00000000000017"  # second valid GTIN


def impact(scans, rules, target_unit=None):
    body = {"scans": scans, "rules": rules}
    if target_unit is not None:
        body["target_unit"] = target_unit
    return client.post("/recall-impact", json=body)


def scan(gtin=GTIN_OK, expiry="250131", serial="SN1", batch=None, weight="3103002000"):
    """Build an element-string scan with 01/17/21, optional 10 and a weight.

    The default weight 3103 002000 is exactly 2 kg.
    """
    parts = [f"01{gtin}", f"17{expiry}"]
    if batch is not None:
        parts.append(f"10{batch}{GS}")
    parts.append(f"21{serial}{GS}")
    parts.append(weight)
    return "".join(parts)


def rule(gtin=GTIN_OK, batches=None, expiry_from=None, expiry_to=None):
    r = {"gtin": gtin}
    if batches is not None:
        r["batches"] = batches
    if expiry_from is not None:
        r["expiry_from"] = expiry_from
    if expiry_to is not None:
        r["expiry_to"] = expiry_to
    return r


# ---------------------------------------------------------------- happy path


def test_gtin_only_rule_matches_every_item_of_that_gtin():
    resp = impact(
        [
            scan(serial="SN1"),
            scan(serial="SN2", weight="3103003000"),
            scan(gtin=GTIN2, serial="SN1"),  # same serial, other GTIN
        ],
        [rule()],
    )
    assert resp.status_code == 200
    body = resp.json()
    (r,) = body["rules"]
    assert r["package_count"] == 2
    assert r["total_weight"] == 5.0
    assert body["unmatched"]["package_count"] == 1
    assert body["unmatched"]["total_weight"] == 2.0


def test_first_matching_rule_claims_the_item():
    resp = impact(
        [scan(serial="SN1", batch="B1")],
        [rule(), rule(batches=["B1"]), rule(gtin=GTIN2)],
    )
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert [r["package_count"] for r in rules] == [1, 0, 0]
    assert rules[0]["total_weight"] == 2.0
    assert rules[1]["total_weight"] == 0.0
    assert resp.json()["unmatched"]["package_count"] == 0


def test_rules_are_reported_in_request_order():
    resp = impact(
        [scan(serial="SN1"), scan(gtin=GTIN2, serial="SN9")],
        [rule(gtin=GTIN2), rule()],
    )
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert [r["rule_index"] for r in rules] == [0, 1]
    assert [r["gtin"] for r in rules] == [GTIN2, GTIN_OK]
    assert [r["package_count"] for r in rules] == [1, 1]


def test_batch_set_matching():
    resp = impact(
        [
            scan(serial="SN1", batch="A"),
            scan(serial="SN2", batch="B"),
            scan(serial="SN3", batch="C"),
            scan(serial="SN4"),  # no batch
        ],
        [rule(batches=["A", "B"])],
    )
    assert resp.status_code == 200
    r = resp.json()["rules"][0]
    assert r["package_count"] == 2
    assert r["total_weight"] == 4.0
    unmatched = resp.json()["unmatched"]
    assert unmatched["package_count"] == 2
    assert unmatched["total_weight"] == 4.0


def test_null_batch_entry_matches_batchless_item():
    resp = impact(
        [scan(serial="SN1"), scan(serial="SN2", batch="A")],
        [rule(batches=[None])],
    )
    assert resp.status_code == 200
    r = resp.json()["rules"][0]
    assert r["package_count"] == 1
    assert resp.json()["unmatched"]["package_count"] == 1


def test_rule_without_batches_accepts_any_batch():
    resp = impact(
        [scan(serial="SN1", batch="A"), scan(serial="SN2")],
        [rule()],
    )
    assert resp.json()["rules"][0]["package_count"] == 2


def test_empty_batch_set_matches_nothing():
    resp = impact([scan(serial="SN1", batch="A")], [rule(batches=[])])
    assert resp.status_code == 200
    assert resp.json()["rules"][0]["package_count"] == 0
    assert resp.json()["unmatched"]["package_count"] == 1


def test_expiry_closed_interval_bounds_are_inclusive():
    resp = impact(
        [
            scan(serial="SN1", expiry="250131"),  # before the interval
            scan(serial="SN2", expiry="250201"),  # lower boundary
            scan(serial="SN3", expiry="250228"),  # upper boundary
            scan(serial="SN4", expiry="250301"),  # after the interval
        ],
        [rule(expiry_from="250201", expiry_to="250228")],
    )
    assert resp.status_code == 200
    r = resp.json()["rules"][0]
    assert r["package_count"] == 2
    assert r["total_weight"] == 4.0
    assert resp.json()["unmatched"]["package_count"] == 2


def test_expiry_bound_day_00_means_last_day_of_month():
    # 250200 denotes 2025-02-28, so the 250228 item is inside and 250301 out.
    resp = impact(
        [scan(serial="SN1", expiry="250228"), scan(serial="SN2", expiry="250301")],
        [rule(expiry_from="250101", expiry_to="250200")],
    )
    assert resp.json()["rules"][0]["package_count"] == 1
    assert resp.json()["unmatched"]["package_count"] == 1


def test_item_day_00_expiry_matches_by_actual_date():
    # The item's 250200 is the actual date 2025-02-28: inside [250201, 250228].
    resp = impact(
        [scan(serial="SN1", expiry="250200")],
        [rule(expiry_from="250201", expiry_to="250228")],
    )
    assert resp.json()["rules"][0]["package_count"] == 1


def test_one_sided_expiry_bounds():
    resp = impact(
        [scan(serial="SN1", expiry="250131"), scan(serial="SN2", expiry="250201")],
        [rule(expiry_from="250201")],
    )
    assert resp.json()["rules"][0]["package_count"] == 1
    resp = impact(
        [scan(serial="SN1", expiry="250131"), scan(serial="SN2", expiry="250201")],
        [rule(expiry_to="250131")],
    )
    assert resp.json()["rules"][0]["package_count"] == 1


def test_combined_gtin_batch_and_expiry_scope():
    resp = impact(
        [
            scan(serial="SN1", batch="B1", expiry="250131"),
            scan(serial="SN2", batch="B1", expiry="250331"),  # expiry outside
            scan(serial="SN3", batch="B9", expiry="250131"),  # batch outside
            scan(gtin=GTIN2, serial="SN4", batch="B1", expiry="250131"),
        ],
        [rule(batches=["B1"], expiry_from="250101", expiry_to="250228")],
    )
    r = resp.json()["rules"][0]
    assert r["package_count"] == 1
    assert resp.json()["unmatched"]["package_count"] == 3


def test_identical_rescan_is_counted_once():
    resp = impact([scan(serial="SN1", batch="B1")] * 3, [rule()])
    assert resp.status_code == 200
    r = resp.json()["rules"][0]
    assert r["package_count"] == 1
    assert r["total_weight"] == 2.0


def test_same_item_as_element_string_and_digital_link_counts_once():
    resp = impact(
        [
            scan(serial="SN1", batch="B1"),
            "https://id.gs1.org/01/09501101530003/17/250131/21/SN1/3103/002000?10=B1",
        ],
        [rule()],
    )
    assert resp.status_code == 200
    r = resp.json()["rules"][0]
    assert r["package_count"] == 1
    assert r["total_weight"] == 2.0


def test_weight_precision_variant_is_the_same_measurement():
    # 3103 002000 = 2 kg and 3104 020000 = 2.0000 kg: one measurement.
    resp = impact(
        [scan(serial="SN1", weight="3103002000"), scan(serial="SN1", weight="3104020000")],
        [rule()],
    )
    assert resp.status_code == 200
    r = resp.json()["rules"][0]
    assert r["package_count"] == 1
    assert r["total_weight"] == 2.0


def test_digital_link_scans_accepted():
    resp = impact(
        ["https://id.gs1.org/01/09501101530003/17/250131/21/SN7/3103/002000?10=B1"],
        [rule(batches=["B1"])],
    )
    assert resp.status_code == 200
    assert resp.json()["rules"][0]["package_count"] == 1


def test_unmatched_aggregates_count_and_weight():
    resp = impact(
        [
            scan(serial="SN1"),  # matched
            scan(serial="SN2", weight="3103003000"),  # 3 kg unmatched
            scan(gtin=GTIN2, serial="SN3", weight="3202002513"),  # 25.13 lb
        ],
        [rule(batches=None)],  # matches every GTIN_OK item
    )
    assert resp.status_code == 200
    unmatched = resp.json()["unmatched"]
    assert unmatched["package_count"] == 1
    # only the GTIN2 item is unmatched: 25.13 lb * 0.45359237
    assert unmatched["total_weight"] == 11.398776


def test_empty_scans_give_zero_tallies():
    resp = impact([], [rule(), rule(gtin=GTIN2)])
    assert resp.status_code == 200
    body = resp.json()
    assert [r["package_count"] for r in body["rules"]] == [0, 0]
    assert all(r["total_weight"] == 0.0 for r in body["rules"])
    assert body["unmatched"] == {
        "package_count": 0,
        "unit": "kg",
        "total_weight": 0.0,
    }


def test_empty_rules_send_everything_to_unmatched():
    resp = impact([scan(serial="SN1"), scan(serial="SN2")], [])
    assert resp.status_code == 200
    body = resp.json()
    assert body["rules"] == []
    assert body["unmatched"]["package_count"] == 2
    assert body["unmatched"]["total_weight"] == 4.0


def test_rule_gtin_needs_no_valid_check_digit():
    # Rule GTINs are plain match criteria, not parsed scans.
    resp = impact([scan(serial="SN1")], [rule(gtin="09501101530004")])
    assert resp.status_code == 200
    assert resp.json()["rules"][0]["package_count"] == 0
    assert resp.json()["unmatched"]["package_count"] == 1


def test_response_shape():
    resp = impact([scan(serial="SN1", batch="B1")], [rule(batches=["B1"])])
    body = resp.json()
    assert set(body) == {"target_unit", "rules", "unmatched"}
    assert set(body["rules"][0]) == {
        "rule_index",
        "gtin",
        "batches",
        "expiry_from",
        "expiry_to",
        "package_count",
        "unit",
        "total_weight",
    }
    assert body["rules"][0]["batches"] == ["B1"]
    assert body["rules"][0]["expiry_from"] is None
    assert body["rules"][0]["expiry_to"] is None
    assert set(body["unmatched"]) == {"package_count", "unit", "total_weight"}


def test_default_target_unit_is_kg():
    resp = impact([scan(serial="SN1")], [rule()])
    body = resp.json()
    assert body["target_unit"] == "kg"
    assert body["rules"][0]["unit"] == "kg"
    assert body["unmatched"]["unit"] == "kg"


# ------------------------------------------------------- units and conversion


def test_lb_item_converted_to_kg_target():
    # 25.13 lb * 0.45359237 = 11.3987762581 kg
    resp = impact([scan(serial="SN1", weight="3202002513")], [rule()])
    r = resp.json()["rules"][0]
    assert r["unit"] == "kg"
    assert r["total_weight"] == 11.398776


def test_kg_item_converted_to_lb_target():
    # 1 kg / 0.45359237 = 2.2046226218... lb
    resp = impact([scan(serial="SN1", weight="3100000001")], [rule()], target_unit="lb")
    r = resp.json()["rules"][0]
    assert r["unit"] == "lb"
    assert r["total_weight"] == 2.204623


def test_mixed_units_summed_per_rule():
    # 2 kg + 25.13 lb = 2 + 11.3987762581 = 13.3987762581 kg over two items
    resp = impact(
        [scan(serial="SN1"), scan(serial="SN2", weight="3202002513")],
        [rule()],
    )
    r = resp.json()["rules"][0]
    assert r["package_count"] == 2
    assert r["total_weight"] == 13.398776


def test_unmatched_total_converted_to_target_unit():
    resp = impact(
        [scan(serial="SN1", weight="3100000001")],  # 1 kg, no rule matches
        [rule(gtin=GTIN2)],
        target_unit="lb",
    )
    unmatched = resp.json()["unmatched"]
    assert unmatched["unit"] == "lb"
    assert unmatched["total_weight"] == 2.204623


# ------------------------------------------------- exact Decimal arithmetic


def test_totals_are_exact_decimal_in_direct_result():
    from app.recall import assess

    result = assess(
        [scan(serial="SN1"), scan(serial="SN2", weight="3202002513")],
        "kg",
        [(GTIN_OK, None, None, None)],
    )
    r = result["rules"][0]
    assert r["total_weight"] == Decimal("13.3987762581")
    assert not isinstance(r["total_weight"], float)
    assert result["unmatched"]["total_weight"] == Decimal(0)


def test_totals_render_as_six_place_json_numbers():
    resp = impact([scan(serial="SN1"), scan(serial="SN2")], [rule()])
    assert '"total_weight":4.000000' in resp.text
    assert '"total_weight":"4.000000"' not in resp.text
    assert '"package_count":2' in resp.text
    assert '"total_weight":0.000000' in resp.text  # unmatched


def test_half_even_rounding_at_six_places():
    # 0.0000005 kg -> nearest even: 0.000000
    resp = impact([scan(serial="SN1", weight="3107000005")], [rule()])
    assert '"total_weight":0.000000' in resp.text
    # 0.0000015 kg -> half-even rounds up to 0.000002
    resp = impact([scan(serial="SN1", weight="3107000015")], [rule()])
    assert '"total_weight":0.000002' in resp.text


def test_rounding_happens_after_summing_not_per_item():
    # Each item is 0.0000004 kg -> 0.000000 individually, but the pair
    # sums to 0.0000008 -> 0.000001.
    resp = impact(
        [scan(serial="SN1", weight="3107000004"), scan(serial="SN2", weight="3107000004")],
        [rule()],
    )
    assert '"total_weight":0.000001' in resp.text


# ------------------------------------------------------------------ conflicts


def test_batch_conflict_reports_current_and_first_index():
    resp = impact(
        [scan(serial="SN1", batch="B1"), scan(serial="SN1", batch="B2")],
        [rule()],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "CONFLICTING_BATCH"
    assert "B1" in detail["error"]["expected"]
    assert "B2" in detail["error"]["found"]
    assert set(detail) == {"scan_index", "first_index", "error"}


def test_batch_absent_then_present_is_a_conflict():
    resp = impact([scan(serial="SN1"), scan(serial="SN1", batch="B1")], [rule()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CONFLICTING_BATCH"
    assert "no batch" in detail["error"]["expected"]


def test_expiry_conflict():
    resp = impact(
        [scan(serial="SN1", expiry="250131"), scan(serial="SN1", expiry="250228")],
        [rule()],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CONFLICTING_EXPIRY"
    assert "250131" in detail["error"]["expected"]
    assert "250228" in detail["error"]["found"]


def test_day_00_and_explicit_last_day_are_a_raw_expiry_conflict():
    # 250200 and 250228 denote the same actual date but differ as scanned.
    resp = impact(
        [scan(serial="SN1", expiry="250200"), scan(serial="SN1", expiry="250228")],
        [rule()],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CONFLICTING_EXPIRY"


def test_weight_conflict():
    resp = impact(
        [scan(serial="SN1", weight="3103002000"), scan(serial="SN1", weight="3103003000")],
        [rule()],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CONFLICTING_WEIGHT"
    assert "2" in detail["error"]["expected"]
    assert "3" in detail["error"]["found"]


def test_weight_unit_conflict_even_at_zero():
    # 0 kg and 0 lb carry different units: the re-scan disagrees.
    resp = impact(
        [scan(serial="SN1", weight="3103000000"), scan(serial="SN1", weight="3203000000")],
        [rule()],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CONFLICTING_WEIGHT"


def test_conflict_found_across_non_adjacent_scans():
    resp = impact(
        [
            scan(serial="SN1", batch="B1"),
            scan(serial="SN2"),
            scan(serial="SN3"),
            scan(serial="SN1", batch="B2"),  # conflicts with index 0
        ],
        [rule()],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 3
    assert detail["first_index"] == 0


def test_same_serial_under_different_gtins_are_distinct_items():
    resp = impact(
        [scan(serial="SN1", batch="B1"), scan(gtin=GTIN2, serial="SN1", batch="B2")],
        [rule(), rule(gtin=GTIN2)],
    )
    assert resp.status_code == 200
    assert [r["package_count"] for r in resp.json()["rules"]] == [1, 1]


def test_same_gtin_with_different_serials_are_distinct_items():
    resp = impact(
        [scan(serial="SN1", batch="B1"), scan(serial="SN2", batch="B2")],
        [rule()],
    )
    assert resp.status_code == 200
    assert resp.json()["rules"][0]["package_count"] == 2


def test_conflict_beats_rule_evaluation_and_matching():
    # The conflicting pair would both match the rule; the 422 still wins.
    resp = impact(
        [scan(serial="SN1"), scan(serial="SN1", weight="3103003000")],
        [rule()],
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- scan errors


def test_missing_serial_rejected():
    resp = impact([f"01{GTIN_OK}172501313103002000"], [rule()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "21" in detail["error"]["message"]


def test_duplicate_serial_rejected():
    resp = impact([f"01{GTIN_OK}1725013121A{GS}21B{GS}3103002000"], [rule()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "21" in detail["error"]["message"]


def test_missing_expiry_rejected():
    resp = impact([f"01{GTIN_OK}21SN1{GS}3103002000"], [rule()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "17" in detail["error"]["message"]


def test_missing_gtin_rejected():
    resp = impact(["1725013121SN1" + GS + "3103002000"], [rule()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "01" in detail["error"]["message"]


def test_missing_weight_rejected():
    resp = impact([f"01{GTIN_OK}1725013121SN1"], [rule()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "310n/320n" in detail["error"]["message"]


def test_duplicate_weight_rejected():
    resp = impact(
        [f"01{GTIN_OK}1725013121SN1{GS}31030020003202002513"], [rule()]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103" in detail["error"]["found"]
    assert "3202" in detail["error"]["found"]


def test_duplicate_batch_rejected():
    resp = impact(
        [f"01{GTIN_OK}1725013110A{GS}10B{GS}21SN1{GS}3103002000"], [rule()]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"


def test_parser_error_reports_scan_index_and_original_error():
    resp = impact(
        [scan(serial="SN1"), "01095011015300041725013121SN2" + GS + "3103002000"],
        [rule()],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"
    assert detail["error"]["offset"] == 15
    assert set(detail) == {"scan_index", "error"}


# ---------------------------------------------------------------- rule errors


def test_expiry_bound_must_be_six_digits():
    resp = impact([scan(serial="SN1")], [rule(expiry_from="2025-01-01")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["rule_index"] == 0
    assert detail["error"]["error"] == "INVALID_EXPIRY_BOUND"
    assert detail["error"]["found"] == "2025-01-01"
    assert set(detail) == {"rule_index", "error"}


def test_expiry_bound_must_be_a_real_date():
    resp = impact([scan(serial="SN1")], [rule(expiry_to="251331")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["rule_index"] == 0
    assert detail["error"]["error"] == "INVALID_DATE"


def test_inverted_expiry_range_rejected():
    resp = impact(
        [scan(serial="SN1")],
        [rule(expiry_from="250331", expiry_to="250131")],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["rule_index"] == 0
    assert detail["error"]["error"] == "EXPIRY_RANGE_INVERTED"
    assert detail["error"]["found"] == "250331 > 250131"


def test_equal_expiry_bounds_form_a_single_day_interval():
    resp = impact(
        [scan(serial="SN1", expiry="250131"), scan(serial="SN2", expiry="250201")],
        [rule(expiry_from="250131", expiry_to="250131")],
    )
    assert resp.status_code == 200
    assert resp.json()["rules"][0]["package_count"] == 1


def test_rule_error_reports_its_own_index():
    resp = impact(
        [scan(serial="SN1")],
        [rule(), rule(expiry_from="bad"), rule(expiry_to="also-bad")],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["rule_index"] == 1


def test_rules_are_validated_before_scans():
    resp = impact(["not-a-gs1-string"], [rule(expiry_from="bad")])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "INVALID_EXPIRY_BOUND"


# ---------------------------------------------------- request model validation


def test_missing_rules_field_is_422():
    resp = client.post("/recall-impact", json={"scans": []})
    assert resp.status_code == 422


def test_missing_scans_field_is_422():
    resp = client.post("/recall-impact", json={"rules": []})
    assert resp.status_code == 422


def test_missing_gtin_in_rule_is_422():
    resp = client.post("/recall-impact", json={"scans": [], "rules": [{}]})
    assert resp.status_code == 422


def test_empty_gtin_in_rule_is_422():
    resp = impact([], [rule(gtin="")])
    assert resp.status_code == 422


def test_unknown_target_unit_is_422():
    resp = impact([], [rule()], target_unit="stone")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "target_unit"]


def test_non_string_scan_entry_is_422():
    resp = client.post("/recall-impact", json={"scans": [123], "rules": []})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "scans", 0]


def test_rules_not_a_list_is_422():
    resp = client.post(
        "/recall-impact", json={"scans": [], "rules": {"gtin": GTIN_OK}}
    )
    assert resp.status_code == 422
