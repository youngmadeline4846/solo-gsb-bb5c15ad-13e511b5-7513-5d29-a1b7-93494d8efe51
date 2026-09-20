"""End-to-end tests for the /weight-pick-plan endpoint."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from app.summary import KG_TO_LB

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit
GTIN2 = "00000000000017"  # second valid GTIN


def plan(scans, demands, target_unit=None, as_of="2024-01-01"):
    """POST a pick plan; the default as_of keeps every test expiry eligible."""
    body = {"scans": scans, "demands": demands}
    if target_unit is not None:
        body["target_unit"] = target_unit
    if as_of is not None:
        body["as_of"] = as_of
    return client.post("/weight-pick-plan", json=body)


def scan(gtin=GTIN_OK, expiry="250131", batch=None, weight="3103002000"):
    """Build an element-string scan with one 01/17/weight and optional 10.

    The default weight 3103 002000 is exactly 2 kg.
    """
    parts = [f"01{gtin}", f"17{expiry}"]
    if batch is not None:
        parts.append(f"10{batch}{GS}")
    parts.append(weight)
    return "".join(parts)


def demand(gtin=GTIN_OK, quantity="1", min_remaining_days=None, package_mode=None):
    d = {"gtin": gtin, "quantity": quantity}
    if min_remaining_days is not None:
        d["min_remaining_days"] = min_remaining_days
    if package_mode is not None:
        d["package_mode"] = package_mode
    return d


# ---------------------------------------------------------------- happy path


def test_fefo_allocates_earliest_expiry_first():
    resp = plan(
        [
            scan(batch="LATE", expiry="250331"),
            scan(batch="EARLY", expiry="250131"),
            scan(batch="MID", expiry="250228"),
        ],
        [demand(quantity="5")],
    )
    assert resp.status_code == 200
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["expiry"], a["weight"]) for a in d["allocations"]] == [
        ("EARLY", "250131", 2.0),
        ("MID", "250228", 2.0),
        ("LATE", "250331", 1.0),
    ]
    assert d["fulfilled"] == 5.0
    assert d["shortage"] == 0.0


def test_last_group_may_be_partially_allocated():
    resp = plan(
        [scan(batch="A"), scan(batch="B", expiry="250228")],
        [demand(quantity="3")],
    )
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["weight"]) for a in d["allocations"]] == [
        ("A", 2.0),
        ("B", 1.0),
    ]
    assert d["fulfilled"] == 3.0
    assert d["shortage"] == 0.0


def test_shortage_when_supply_insufficient():
    resp = plan([scan(), scan()], [demand(quantity="10")])
    d = resp.json()["demands"][0]
    # identical scans share one (gtin, batch, expiry) group of 4 kg
    assert d["allocations"] == [{"batch": None, "expiry": "250131", "weight": 4.0}]
    assert d["fulfilled"] == 4.0
    assert d["shortage"] == 6.0


def test_empty_scans_produce_full_shortage():
    resp = plan([], [demand(quantity="2.5"), demand(gtin=GTIN2, quantity="1")])
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "kg"
    first, second = body["demands"]
    assert first["fulfilled"] == 0.0
    assert first["shortage"] == 2.5
    assert first["allocations"] == []
    assert second["fulfilled"] == 0.0
    assert second["shortage"] == 1.0
    assert second["allocations"] == []


def test_empty_demands_and_scans():
    resp = plan([], [])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "demands": []}


def test_demand_for_unscanned_gtin_is_full_shortage():
    resp = plan([scan()], [demand(gtin=GTIN2, quantity="2")])
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 0.0
    assert d["shortage"] == 2.0
    assert d["allocations"] == []


def test_day_00_expiry_sorts_as_last_day_of_month():
    # 250200 denotes 2025-02-28, so 250215 expires first.
    resp = plan(
        [scan(batch="MONTH_END", expiry="250200"), scan(batch="MID", expiry="250215")],
        [demand(quantity="3")],
    )
    d = resp.json()["demands"][0]
    assert [a["batch"] for a in d["allocations"]] == ["MID", "MONTH_END"]


def test_day_00_ties_with_explicit_last_day_keep_first_seen_order():
    # 2025 is not a leap year: 250200 and 250228 are both 2025-02-28.
    resp = plan(
        [scan(batch="ZERO", expiry="250200"), scan(batch="EXPLICIT", expiry="250228")],
        [demand(quantity="3")],
    )
    d = resp.json()["demands"][0]
    assert [a["batch"] for a in d["allocations"]] == ["ZERO", "EXPLICIT"]


def test_day_00_leap_year_month_end():
    # 2024 is a leap year: 240200 and 240229 are both 2024-02-29.
    resp = plan(
        [scan(batch="EXPLICIT", expiry="240229"), scan(batch="ZERO", expiry="240200")],
        [demand(quantity="3")],
    )
    d = resp.json()["demands"][0]
    assert [a["batch"] for a in d["allocations"]] == ["EXPLICIT", "ZERO"]


def test_same_expiry_date_keeps_batch_first_seen_order():
    resp = plan(
        [scan(batch="B2"), scan(batch="B1"), scan(batch="B3")],
        [demand(quantity="5")],
    )
    d = resp.json()["demands"][0]
    assert [a["batch"] for a in d["allocations"]] == ["B2", "B1", "B3"]


def test_same_gtin_batch_expiry_scans_are_aggregated():
    resp = plan(
        [scan(batch="A"), scan(batch="A", weight="3103001000")],
        [demand(quantity="2.5")],
    )
    d = resp.json()["demands"][0]
    assert d["allocations"] == [{"batch": "A", "expiry": "250131", "weight": 2.5}]
    assert d["shortage"] == 0.0


def test_same_batch_with_different_expiries_forms_two_groups():
    resp = plan(
        [scan(batch="A", expiry="250228"), scan(batch="A", expiry="250131")],
        [demand(quantity="3")],
    )
    allocs = resp.json()["demands"][0]["allocations"]
    assert [(a["expiry"], a["weight"]) for a in allocs] == [
        ("250131", 2.0),
        ("250228", 1.0),
    ]


def test_zero_weight_group_is_skipped():
    resp = plan(
        [scan(batch="EMPTY", weight="3103000000"), scan(batch="FULL", expiry="250228")],
        [demand(quantity="1")],
    )
    allocs = resp.json()["demands"][0]["allocations"]
    assert allocs == [{"batch": "FULL", "expiry": "250228", "weight": 1.0}]


def test_demands_for_different_gtins_are_independent():
    resp = plan(
        [scan(gtin=GTIN2, expiry="250228"), scan()],
        [demand(quantity="3"), demand(gtin=GTIN2, quantity="3")],
    )
    assert resp.status_code == 200
    first, second = resp.json()["demands"]
    assert first["gtin"] == GTIN_OK
    assert first["fulfilled"] == 2.0
    assert first["shortage"] == 1.0
    assert second["gtin"] == GTIN2
    assert second["fulfilled"] == 2.0
    assert second["shortage"] == 1.0


def test_digital_link_scans_accepted():
    resp = plan(
        ["https://id.gs1.org/01/09501101530003/17/250131/3103/002000?10=B1"],
        [demand(quantity="2")],
    )
    assert resp.status_code == 200
    alloc = resp.json()["demands"][0]["allocations"][0]
    assert alloc == {"batch": "B1", "expiry": "250131", "weight": 2.0}


def test_serial_number_ai_21_is_ignored():
    resp = plan(
        [f"01{GTIN_OK}1725013121SN0001{GS}3103002000"],
        [demand(quantity="2")],
    )
    assert resp.status_code == 200
    assert resp.json()["demands"][0]["fulfilled"] == 2.0


def test_response_shape():
    resp = plan([scan(batch="A")], [demand(quantity="1")])
    body = resp.json()
    assert set(body) == {"target_unit", "demands"}
    d = body["demands"][0]
    assert set(d) == {
        "gtin",
        "quantity",
        "fulfilled",
        "shortage",
        "overage",
        "excluded_weight",
        "allocations",
    }
    assert set(d["allocations"][0]) == {"batch", "expiry", "weight"}


def test_splittable_overage_is_always_zero():
    resp = plan([scan(), scan(batch="B", expiry="250228")], [demand(quantity="3")])
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 3.0
    assert d["overage"] == 0.0
    assert '"overage":0.000000' in resp.text


# ------------------------------------------------------- units and conversion


def test_lb_scans_converted_for_kg_target():
    # 25.13 lb * 0.45359237 = 11.3987762581 kg, demanded exactly.
    resp = plan([scan(weight="3202002513")], [demand(quantity="11.3987762581")])
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 11.398776  # six-place rendering
    assert d["shortage"] == 0.0


def test_kg_scan_fulfills_lb_demand_exactly():
    resp = plan(
        [scan(weight="3100000001")],  # 1 kg
        [demand(quantity=str(KG_TO_LB))],
        target_unit="lb",
    )
    d = resp.json()["demands"][0]
    assert d["shortage"] == 0.0
    assert d["fulfilled"] == 2.204623  # rounded only at the response


def test_mixed_units_aggregate_before_allocation():
    # one group: 2 kg + 25.13 lb = 13.3987762581 kg available
    resp = plan(
        [scan(batch="A"), scan(batch="A", weight="3202002513")],
        [demand(quantity="13")],
    )
    d = resp.json()["demands"][0]
    assert d["allocations"] == [{"batch": "A", "expiry": "250131", "weight": 13.0}]
    assert d["shortage"] == 0.0


def test_default_target_unit_is_kg():
    resp = plan([scan()], [demand(quantity="1")])
    assert resp.json()["target_unit"] == "kg"


# ------------------------------------------------- exact Decimal arithmetic


def test_allocation_math_is_exact_decimal():
    from app.pickplan import pick_plan

    result = pick_plan(
        [scan(weight="3202002513")],  # 11.3987762581 kg exactly
        "kg",
        [(GTIN_OK, Decimal("11.3987762581"), 0)],
        date(2024, 1, 1),
    )
    d = result["demands"][0]
    assert d["fulfilled"] == Decimal("11.3987762581")
    assert d["shortage"] == Decimal(0)
    assert d["excluded_weight"] == Decimal(0)
    assert d["allocations"][0]["weight"] == Decimal("11.3987762581")
    assert d["fulfilled"] + d["shortage"] == d["quantity"]
    assert not isinstance(d["fulfilled"], float)


def test_json_float_quantity_is_exact():
    # 2.513 as a JSON number must not acquire binary-float noise.
    resp = plan([scan(weight="3103002513")], [{"gtin": GTIN_OK, "quantity": 2.513}])
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 2.513
    assert d["shortage"] == 0.0


def test_weights_render_as_six_place_json_numbers():
    resp = plan([scan(weight="3103002513")], [demand(quantity="2.513")])
    assert '"quantity":2.513000' in resp.text
    assert '"fulfilled":2.513000' in resp.text
    assert '"shortage":0.000000' in resp.text
    assert '"weight":2.513000' in resp.text
    assert '"fulfilled":"2.513000"' not in resp.text


def test_half_even_rounding_only_at_response():
    # 0.0000005 kg -> nearest even at six places: 0.000000
    resp = plan([scan(weight="3107000005")], [demand(quantity="0.0000005")])
    assert '"fulfilled":0.000000' in resp.text
    # 0.0000015 kg -> half-even rounds up to 0.000002
    resp = plan([scan(weight="3107000015")], [demand(quantity="0.0000015")])
    assert '"fulfilled":0.000002' in resp.text


# ---------------------------------------------------------------- scan errors


def test_missing_expiry_rejected():
    resp = plan([f"01{GTIN_OK}3103002000"], [demand()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "17" in detail["error"]["message"]


def test_duplicate_expiry_rejected():
    resp = plan([f"01{GTIN_OK}17250131172501313103002000"], [demand()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert detail["error"]["offset"] == 24  # offset of the second AI 17


def test_missing_gtin_rejected():
    resp = plan(["172501313103002000"], [demand()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "01" in detail["error"]["message"]


def test_missing_weight_rejected():
    resp = plan([f"01{GTIN_OK}17250131"], [demand()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "310n/320n" in detail["error"]["message"]


def test_duplicate_weight_rejected():
    resp = plan([f"01{GTIN_OK}1725013131030020003202002513"], [demand()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103" in detail["error"]["found"]
    assert "3202" in detail["error"]["found"]


def test_parser_error_reports_scan_index_and_original_error():
    resp = plan([scan(), "0109501101530004172501313103002000"], [demand()])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"
    assert detail["error"]["offset"] == 15
    assert set(detail) == {"scan_index", "error"}


# -------------------------------------------------------------- demand errors


def test_duplicate_demand_gtin_rejected():
    resp = plan([scan()], [demand(quantity="1"), demand(quantity="2")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 1
    assert detail["error"]["error"] == "DUPLICATE_GTIN"
    assert detail["error"]["found"] == GTIN_OK
    assert set(detail) == {"demand_index", "error"}


def test_duplicate_demand_gtin_checked_before_scans():
    resp = plan(["not-a-gs1-string"], [demand(), demand()])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_GTIN"


# ---------------------------------------------------- shelf-life threshold


def test_as_of_is_required():
    resp = plan([scan()], [demand()], as_of=None)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "as_of"]


def test_invalid_as_of_rejected():
    for bad in ("2025-13-01", "2025-02-30", "not-a-date", "250131", "2025-1-5"):
        resp = plan([scan()], [demand()], as_of=bad)
        assert resp.status_code == 422, bad


def test_non_string_as_of_rejected():
    resp = client.post(
        "/weight-pick-plan",
        json={"scans": [], "demands": [], "as_of": 20250131},
    )
    assert resp.status_code == 422


def test_default_threshold_excludes_stock_expiring_before_as_of():
    resp = plan(
        [scan(batch="OLD", expiry="250131"), scan(batch="FRESH", expiry="250228")],
        [demand(quantity="2")],
        as_of="2025-02-01",
    )
    assert resp.status_code == 200
    d = resp.json()["demands"][0]
    assert d["allocations"] == [{"batch": "FRESH", "expiry": "250228", "weight": 2.0}]
    assert d["fulfilled"] == 2.0
    assert d["shortage"] == 0.0
    assert d["excluded_weight"] == 2.0


def test_expiry_on_threshold_date_is_eligible():
    # as_of 2025-01-16 + 15 days = 2025-01-31: the boundary date participates.
    resp = plan(
        [scan(batch="EDGE", expiry="250131")],
        [demand(quantity="2", min_remaining_days=15)],
        as_of="2025-01-16",
    )
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 2.0
    assert d["excluded_weight"] == 0.0


def test_expiry_one_day_before_threshold_is_excluded():
    resp = plan(
        [scan(batch="SOON", expiry="250130")],
        [demand(quantity="2", min_remaining_days=15)],
        as_of="2025-01-16",  # threshold 2025-01-31
    )
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 0.0
    assert d["shortage"] == 2.0
    assert d["allocations"] == []
    assert d["excluded_weight"] == 2.0


def test_min_remaining_days_crosses_month_boundary():
    # as_of 2025-01-20 + 20 days = 2025-02-09.
    resp = plan(
        [scan(batch="JAN", expiry="250131"), scan(batch="FEB", expiry="250210")],
        [demand(quantity="2", min_remaining_days=20)],
        as_of="2025-01-20",
    )
    d = resp.json()["demands"][0]
    assert [a["batch"] for a in d["allocations"]] == ["FEB"]
    assert d["excluded_weight"] == 2.0


def test_day_00_month_end_compared_against_threshold():
    # 250200 denotes 2025-02-28: eligible on 2025-02-28, excluded one day later.
    resp = plan(
        [scan(batch="MONTH_END", expiry="250200")],
        [demand(quantity="2")],
        as_of="2025-02-28",
    )
    assert resp.json()["demands"][0]["fulfilled"] == 2.0
    resp = plan(
        [scan(batch="MONTH_END", expiry="250200")],
        [demand(quantity="2", min_remaining_days=1)],
        as_of="2025-02-28",  # threshold 2025-03-01
    )
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 0.0
    assert d["excluded_weight"] == 2.0


def test_fefo_order_preserved_among_eligible_groups():
    resp = plan(
        [
            scan(batch="EXCLUDED", expiry="250110"),
            scan(batch="LATE", expiry="250331"),
            scan(batch="MID", expiry="250228"),
        ],
        [demand(quantity="3")],
        as_of="2025-02-01",
    )
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["weight"]) for a in d["allocations"]] == [
        ("MID", 2.0),
        ("LATE", 1.0),
    ]
    assert d["excluded_weight"] == 2.0


def test_excluded_weight_sums_groups_and_converts_units():
    resp = plan(
        [
            scan(batch="A", expiry="250110"),  # 2 kg excluded
            scan(batch="B", expiry="250115", weight="3202002513"),  # 25.13 lb excluded
            scan(batch="C", expiry="250331"),  # eligible
        ],
        [demand(quantity="1")],
        as_of="2025-02-01",
    )
    d = resp.json()["demands"][0]
    # 2 kg + 25.13 lb * 0.45359237 = 13.3987762581 kg -> 13.398776
    assert d["excluded_weight"] == 13.398776
    assert '"excluded_weight":13.398776' in resp.text


def test_excluded_weight_counts_only_the_demands_gtin():
    resp = plan(
        [scan(gtin=GTIN2, expiry="250110"), scan(expiry="250331")],
        [demand(quantity="1")],
        as_of="2025-02-01",
    )
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 1.0
    assert d["excluded_weight"] == 0.0


def test_excluded_stock_never_counts_toward_fulfilled():
    resp = plan(
        [
            scan(batch="OLD", expiry="250110"),
            scan(batch="OK", expiry="250228", weight="3103001000"),
        ],
        [demand(quantity="2")],
        as_of="2025-02-01",
    )
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 1.0
    assert d["shortage"] == 1.0
    assert d["excluded_weight"] == 2.0


def test_excluded_weight_rounds_half_even_at_six_places():
    # excluded 0.0000005 kg -> nearest even at six places: 0.000000
    resp = plan(
        [scan(expiry="250110", weight="3107000005")],
        [demand(quantity="1")],
        as_of="2025-02-01",
    )
    assert '"excluded_weight":0.000000' in resp.text
    # excluded 0.0000015 kg -> half-even rounds up to 0.000002
    resp = plan(
        [scan(expiry="250110", weight="3107000015")],
        [demand(quantity="1")],
        as_of="2025-02-01",
    )
    assert '"excluded_weight":0.000002' in resp.text


def test_excluded_weight_is_exact_decimal_in_direct_result():
    from app.pickplan import pick_plan

    result = pick_plan(
        [scan(expiry="250110"), scan(expiry="250331")],
        "kg",
        [(GTIN_OK, Decimal("2"), 0)],
        date(2025, 2, 1),
    )
    d = result["demands"][0]
    assert d["excluded_weight"] == Decimal(2)
    assert d["fulfilled"] == Decimal(2)
    assert not isinstance(d["excluded_weight"], float)


def test_huge_min_remaining_days_excludes_everything():
    resp = plan(
        [scan()],
        [demand(quantity="2", min_remaining_days=10**15)],
        as_of="2025-01-01",
    )
    assert resp.status_code == 200
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 0.0
    assert d["shortage"] == 2.0
    assert d["excluded_weight"] == 2.0


# ---------------------------------------------------- min_remaining_days errors


def test_negative_min_remaining_days_rejected_with_demand_index():
    resp = plan(
        [scan()],
        [demand(), demand(gtin=GTIN2, min_remaining_days=-1)],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 1
    assert detail["error"]["error"] == "INVALID_MIN_REMAINING_DAYS"
    assert detail["error"]["found"] == "-1"
    assert set(detail) == {"demand_index", "error"}


def test_negative_min_remaining_days_checked_before_scans():
    resp = plan(["not-a-gs1-string"], [demand(min_remaining_days=-3)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 0
    assert detail["error"]["error"] == "INVALID_MIN_REMAINING_DAYS"


def test_zero_min_remaining_days_is_the_default():
    resp = plan([scan()], [demand(quantity="2", min_remaining_days=0)], as_of="2025-01-31")
    assert resp.status_code == 200
    assert resp.json()["demands"][0]["fulfilled"] == 2.0


def test_non_integer_min_remaining_days_is_422():
    resp = plan(
        [scan()],
        [{"gtin": GTIN_OK, "quantity": 1, "min_remaining_days": 1.5}],
    )
    assert resp.status_code == 422


# ---------------------------------------------------- request model validation


def test_missing_demands_field_is_422():
    resp = client.post("/weight-pick-plan", json={"scans": []})
    assert resp.status_code == 422


def test_missing_scans_field_is_422():
    resp = client.post("/weight-pick-plan", json={"demands": []})
    assert resp.status_code == 422


def test_zero_or_negative_quantity_is_422():
    assert plan([], [demand(quantity="0")]).status_code == 422
    assert plan([], [demand(quantity="-1")]).status_code == 422


def test_unknown_target_unit_is_422():
    resp = plan([], [demand()], target_unit="stone")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "target_unit"]


def test_non_string_scan_entry_is_422():
    resp = client.post(
        "/weight-pick-plan",
        json={"scans": [123], "demands": [demand()]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "scans", 0]


# ---------------------------------------------------------- whole package_mode


def whole_demand(gtin=GTIN_OK, quantity="1", min_remaining_days=None):
    return demand(
        gtin=gtin,
        quantity=quantity,
        min_remaining_days=min_remaining_days,
        package_mode="whole",
    )


def test_whole_mode_picks_subset_with_smallest_overage():
    # FEFO order weights 2, 3, 4 kg; subsets reaching 5: {2,3}=5, {2,4}=6, {3,4}=7.
    resp = plan(
        [
            scan(batch="A", expiry="250131", weight="3103002000"),
            scan(batch="B", expiry="250228", weight="3103003000"),
            scan(batch="C", expiry="250331", weight="3103004000"),
        ],
        [whole_demand(quantity="5")],
    )
    assert resp.status_code == 200
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["weight"]) for a in d["allocations"]] == [
        ("A", 2.0),
        ("B", 3.0),
    ]
    assert d["fulfilled"] == 5.0
    assert d["shortage"] == 0.0
    assert d["overage"] == 0.0


def test_whole_mode_overage_when_no_exact_subset():
    resp = plan(
        [scan(batch="A", weight="3103003000"), scan(batch="B", weight="3103003000")],
        [whole_demand(quantity="4")],
    )
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 6.0
    assert d["overage"] == 2.0
    assert d["shortage"] == 0.0  # never negative
    assert '"overage":2.000000' in resp.text
    assert '"shortage":0.000000' in resp.text


def test_whole_mode_prefers_overage_over_fefo_order():
    # FEFO order weights 2, 5 kg; {5} overshoots less than {2,5}.
    resp = plan(
        [
            scan(batch="EARLY", expiry="250131", weight="3103002000"),
            scan(batch="LATE", expiry="250228", weight="3103005000"),
        ],
        [whole_demand(quantity="4")],
    )
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["weight"]) for a in d["allocations"]] == [("LATE", 5.0)]
    assert d["overage"] == 1.0


def test_whole_mode_tie_breaks_by_lexicographic_fefo_index_sequence():
    # FEFO order is A, B, C (by expiry, not scan order); every pair totals
    # 4 kg, and [A, B] is the lexicographically smallest index sequence.
    resp = plan(
        [
            scan(batch="C", expiry="250331"),
            scan(batch="B", expiry="250228"),
            scan(batch="A", expiry="250131"),
        ],
        [whole_demand(quantity="4")],
    )
    d = resp.json()["demands"][0]
    assert [a["batch"] for a in d["allocations"]] == ["A", "B"]
    assert d["fulfilled"] == 4.0
    assert d["overage"] == 0.0


def test_whole_mode_allocations_are_one_per_scan_not_aggregated():
    # Two identical scans share one (gtin, batch, expiry) group, but whole
    # mode treats them as two indivisible 2 kg packages.
    resp = plan([scan(batch="S"), scan(batch="S")], [whole_demand(quantity="3")])
    d = resp.json()["demands"][0]
    assert d["allocations"] == [
        {"batch": "S", "expiry": "250131", "weight": 2.0},
        {"batch": "S", "expiry": "250131", "weight": 2.0},
    ]
    assert d["fulfilled"] == 4.0
    assert d["overage"] == 1.0
    assert d["shortage"] == 0.0


def test_whole_mode_takes_everything_when_quantity_unreachable():
    resp = plan(
        [scan(batch="A", expiry="250228"), scan(batch="B", expiry="250131")],
        [whole_demand(quantity="10")],
    )
    d = resp.json()["demands"][0]
    # all eligible packages, returned in FEFO order
    assert [a["batch"] for a in d["allocations"]] == ["B", "A"]
    assert d["fulfilled"] == 4.0
    assert d["shortage"] == 6.0
    assert d["overage"] == 0.0


def test_whole_mode_empty_scans_are_full_shortage():
    resp = plan([], [whole_demand(quantity="2.5")])
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 0.0
    assert d["shortage"] == 2.5
    assert d["overage"] == 0.0
    assert d["allocations"] == []


def test_whole_mode_respects_shelf_life_threshold():
    # as_of 2025-01-16 + 15 days = 2025-01-31: OLD (2025-01-30) is excluded.
    resp = plan(
        [
            scan(batch="OLD", expiry="250130"),
            scan(batch="P1", expiry="250131", weight="3103003000"),
            scan(batch="P2", expiry="250228", weight="3103004000"),
        ],
        [whole_demand(quantity="5", min_remaining_days=15)],
        as_of="2025-01-16",
    )
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["weight"]) for a in d["allocations"]] == [
        ("P1", 3.0),
        ("P2", 4.0),
    ]
    assert d["fulfilled"] == 7.0
    assert d["overage"] == 2.0
    assert d["excluded_weight"] == 2.0


def test_whole_mode_converts_units_with_exact_decimal():
    # 25.13 lb * 0.45359237 = 11.3987762581 kg, taken as one package.
    resp = plan([scan(weight="3202002513")], [whole_demand(quantity="11")])
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 11.398776
    assert d["overage"] == 0.398776
    assert d["shortage"] == 0.0
    assert '"overage":0.398776' in resp.text


def test_whole_mode_zero_weight_package_joins_lexicographically_smallest_subset():
    # Subsets reaching 2 kg: {FULL} and {EMPTY,FULL} both total exactly 2;
    # the index sequence [0, 1] is lexicographically smaller than [1].
    resp = plan(
        [
            scan(batch="EMPTY", expiry="250131", weight="3103000000"),
            scan(batch="FULL", expiry="250228"),
        ],
        [whole_demand(quantity="2")],
    )
    d = resp.json()["demands"][0]
    assert [(a["batch"], a["weight"]) for a in d["allocations"]] == [
        ("EMPTY", 0.0),
        ("FULL", 2.0),
    ]
    assert d["fulfilled"] == 2.0
    assert d["overage"] == 0.0


def test_whole_mode_more_than_30_eligible_packages_is_422():
    resp = plan([scan() for _ in range(31)], [whole_demand(quantity="5")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 0
    assert detail["error"]["error"] == "TOO_MANY_PACKAGES"
    assert detail["error"]["found"] == "31"
    assert set(detail) == {"demand_index", "error"}


def test_whole_mode_exactly_30_eligible_packages_is_ok():
    resp = plan([scan() for _ in range(30)], [whole_demand(quantity="5")])
    assert resp.status_code == 200
    d = resp.json()["demands"][0]
    # three 2 kg packages are the smallest-overage subset reaching 5 kg
    assert len(d["allocations"]) == 3
    assert d["fulfilled"] == 6.0
    assert d["overage"] == 1.0


def test_whole_mode_limit_counts_only_eligible_packages():
    # 31 scans but one expires before as_of: 30 eligible -> accepted.
    scans = [scan(expiry="250110")] + [scan(expiry="250331") for _ in range(30)]
    resp = plan(scans, [whole_demand(quantity="5")], as_of="2025-02-01")
    assert resp.status_code == 200
    d = resp.json()["demands"][0]
    assert d["fulfilled"] == 6.0
    assert d["excluded_weight"] == 2.0


def test_whole_mode_limit_is_per_demand_and_reports_its_index():
    scans = [scan()] + [scan(gtin=GTIN2) for _ in range(31)]
    resp = plan(
        scans,
        [demand(quantity="1"), whole_demand(gtin=GTIN2, quantity="5")],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["demand_index"] == 1


def test_splittable_demand_has_no_package_limit():
    resp = plan([scan() for _ in range(31)], [demand(quantity="5")])
    assert resp.status_code == 200
    assert resp.json()["demands"][0]["fulfilled"] == 5.0


def test_mixed_package_modes_in_one_request():
    resp = plan(
        [
            scan(batch="W", weight="3103003000"),
            scan(gtin=GTIN2, batch="S", weight="3103002000"),
            scan(gtin=GTIN2, batch="S2", expiry="250228", weight="3103002000"),
        ],
        [whole_demand(quantity="2"), demand(gtin=GTIN2, quantity="3")],
    )
    assert resp.status_code == 200
    first, second = resp.json()["demands"]
    # whole: the single 3 kg package overshoots less than anything else
    assert first["allocations"] == [{"batch": "W", "expiry": "250131", "weight": 3.0}]
    assert first["overage"] == 1.0
    # splittable: FEFO with a partial final allocation
    assert [(a["batch"], a["weight"]) for a in second["allocations"]] == [
        ("S", 2.0),
        ("S2", 1.0),
    ]
    assert second["overage"] == 0.0


def test_explicit_splittable_mode_matches_default():
    scans = [scan(batch="A"), scan(batch="B", expiry="250228")]
    expected = plan(scans, [demand(quantity="3")]).json()
    explicit = plan(scans, [demand(quantity="3", package_mode="splittable")]).json()
    assert explicit == expected


def test_unknown_package_mode_is_422():
    resp = plan([scan()], [demand(package_mode="half")])
    assert resp.status_code == 422


def test_invalid_package_mode_rejected_in_direct_call():
    import pytest

    from app.pickplan import DemandValidationError, pick_plan

    with pytest.raises(DemandValidationError) as excinfo:
        pick_plan([], "kg", [(GTIN_OK, Decimal(1), 0, "half")], date(2024, 1, 1))
    detail = excinfo.value.detail
    assert detail["demand_index"] == 0
    assert detail["error"]["error"] == "INVALID_PACKAGE_MODE"


def test_whole_mode_is_exact_decimal_in_direct_result():
    from app.pickplan import pick_plan

    result = pick_plan(
        [scan(weight="3202002513")],  # 11.3987762581 kg exactly
        "kg",
        [(GTIN_OK, Decimal("11"), 0, "whole")],
        date(2024, 1, 1),
    )
    d = result["demands"][0]
    assert d["fulfilled"] == Decimal("11.3987762581")
    assert d["overage"] == Decimal("0.3987762581")
    assert d["shortage"] == Decimal(0)
    assert d["allocations"][0]["weight"] == Decimal("11.3987762581")
    assert not isinstance(d["overage"], float)


def test_direct_call_with_3_tuple_still_defaults_to_splittable():
    from app.pickplan import pick_plan

    result = pick_plan(
        [scan()],
        "kg",
        [(GTIN_OK, Decimal("1"), 0)],
        date(2024, 1, 1),
    )
    d = result["demands"][0]
    assert d["fulfilled"] == Decimal(1)
    assert d["overage"] == Decimal(0)
