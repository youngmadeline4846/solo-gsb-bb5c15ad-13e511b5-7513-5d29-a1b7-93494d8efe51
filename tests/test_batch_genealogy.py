"""End-to-end tests for the /batch-genealogy endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_RAW = "09501101530003"  # valid Mod-10 check digit
GTIN_PRODUCT = "00000000000017"  # second valid GTIN


def genealogy(opening_stock=None, processes=None, target_unit=None):
    body = {"opening_stock": opening_stock or [], "processes": processes or []}
    if target_unit is not None:
        body["target_unit"] = target_unit
    return client.post("/batch-genealogy", json=body)


def scan(gtin=GTIN_RAW, batch="R1", weight="3103001000"):
    """Element-string scan with 01/10 and one weight AI (default 1 kg)."""
    return f"01{gtin}10{batch}{GS}{weight}"


def process(inputs, outputs):
    return {"inputs": inputs, "outputs": outputs}


def batches_by_name(body):
    return {entry["batch"]: entry for entry in body["batches"]}


# ---------------------------------------------------------------- happy path


def test_processes_run_in_stable_topological_order():
    # Request order is reversed: process 0 consumes what process 1 produces.
    resp = genealogy(
        opening_stock=[scan(weight="3103010000")],  # 10 kg of R1
        processes=[
            process(
                inputs=[scan(gtin=GTIN_PRODUCT, batch="P1", weight="3103002000")],
                outputs=[scan(gtin=GTIN_PRODUCT, batch="P2", weight="3103001900")],
            ),
            process(
                inputs=[scan(batch="R1", weight="3103006000")],
                outputs=[scan(gtin=GTIN_PRODUCT, batch="P1", weight="3103005500")],
            ),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "kg"
    assert [p["process_index"] for p in body["processes"]] == [1, 0]

    first, second = body["processes"]
    assert first["inputs"] == [{"gtin": GTIN_RAW, "batch": "R1", "weight": 6.0}]
    assert first["outputs"] == [
        {"gtin": GTIN_PRODUCT, "batch": "P1", "weight": 5.5}
    ]
    assert first["input_weight"] == 6.0
    assert first["output_weight"] == 5.5
    assert first["loss_weight"] == 0.5
    assert second["inputs"] == [
        {"gtin": GTIN_PRODUCT, "batch": "P1", "weight": 2.0}
    ]
    assert second["loss_weight"] == 0.1

    batches = batches_by_name(body)
    assert batches["R1"]["remaining"] == 4.0
    assert batches["P1"]["remaining"] == 3.5
    assert batches["P2"]["remaining"] == 1.9


def test_independent_processes_keep_request_order():
    resp = genealogy(
        opening_stock=[
            scan(batch="A", weight="3103005000"),
            scan(batch="B", weight="3103005000"),
        ],
        processes=[
            process(
                inputs=[scan(batch="A", weight="3103001000")],
                outputs=[scan(batch="A2", weight="3103000900")],
            ),
            process(
                inputs=[scan(batch="B", weight="3103001000")],
                outputs=[scan(batch="B2", weight="3103000800")],
            ),
        ],
    )
    assert resp.status_code == 200
    assert [p["process_index"] for p in resp.json()["processes"]] == [0, 1]


def test_ready_processes_run_in_request_index_order():
    resp = genealogy(
        opening_stock=[scan(batch="R", weight="3103005000")],
        processes=[
            process(inputs=[scan(batch="Y")], outputs=[]),  # waits for process 2
            process(inputs=[scan(batch="R")], outputs=[]),  # ready at once
            process(inputs=[], outputs=[scan(batch="Y")]),  # ready at once
        ],
    )
    assert resp.status_code == 200
    assert [p["process_index"] for p in resp.json()["processes"]] == [1, 2, 0]


def test_diamond_dependencies_resolve_stably():
    resp = genealogy(
        opening_stock=[scan(batch="R", weight="3103010000")],
        processes=[
            process(
                inputs=[scan(batch="R", weight="3103010000")],
                outputs=[scan(batch="X", weight="3103009000")],
            ),
            process(
                inputs=[scan(batch="X", weight="3103004000")],
                outputs=[scan(batch="Y", weight="3103003900")],
            ),
            process(
                inputs=[scan(batch="X", weight="3103005000")],
                outputs=[scan(batch="Z", weight="3103004900")],
            ),
            process(
                inputs=[
                    scan(batch="Y", weight="3103003900"),
                    scan(batch="Z", weight="3103004900"),
                ],
                outputs=[scan(batch="W", weight="3103008700")],
            ),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [p["process_index"] for p in body["processes"]] == [0, 1, 2, 3]
    assert body["batches"][-1]["batch"] == "W"
    assert batches_by_name(body)["W"]["remaining"] == 8.7


def test_scans_of_one_batch_are_aggregated():
    resp = genealogy(
        opening_stock=[scan(weight="3103001000"), scan(weight="3103002000")],
        processes=[
            process(
                inputs=[scan(weight="3103001500"), scan(weight="3103000500")],
                outputs=[
                    scan(batch="P1", weight="3103001000"),
                    scan(batch="P1", weight="3103000900"),
                ],
            )
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["processes"][0]["inputs"] == [
        {"gtin": GTIN_RAW, "batch": "R1", "weight": 2.0}
    ]
    assert body["processes"][0]["outputs"] == [
        {"gtin": GTIN_RAW, "batch": "P1", "weight": 1.9}
    ]
    r1 = batches_by_name(body)["R1"]
    assert r1["opening"] == 3.0
    assert r1["consumed"] == 2.0
    assert r1["remaining"] == 1.0


def test_weights_are_converted_to_the_target_unit():
    # 25.13 lb * 0.45359237 = 11.3987762581 kg on both sides: the
    # consumption exactly matches the opening stock, nothing remains.
    resp = genealogy(
        opening_stock=[scan(weight="3202002513")],
        processes=[
            process(
                inputs=[scan(weight="3202002513")],
                outputs=[scan(batch="P1", weight="3103010000")],
            )
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["processes"][0]["input_weight"] == 11.398776
    assert body["processes"][0]["loss_weight"] == 1.398776
    r1 = batches_by_name(body)["R1"]
    assert r1["opening"] == 11.398776
    assert r1["remaining"] == 0.0


def test_target_unit_lb():
    # 1 kg / 0.45359237 = 2.2046226218... lb
    resp = genealogy(opening_stock=[scan(weight="3100000001")], target_unit="lb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "lb"
    assert body["batches"][0]["opening"] == 2.204623
    assert body["batches"][0]["remaining"] == 2.204623


def test_loss_is_negative_when_output_exceeds_input():
    resp = genealogy(
        opening_stock=[scan()],
        processes=[
            process(
                inputs=[scan()],
                outputs=[scan(batch="P1", weight="3103001500")],
            )
        ],
    )
    assert resp.status_code == 200
    process_result = resp.json()["processes"][0]
    assert process_result["input_weight"] == 1.0
    assert process_result["output_weight"] == 1.5
    assert process_result["loss_weight"] == -0.5


def test_produced_weight_adds_to_opening_stock_of_the_same_batch():
    resp = genealogy(
        opening_stock=[scan(batch="A", weight="3103001000")],
        processes=[
            process(inputs=[], outputs=[scan(batch="A", weight="3103002000")]),
            process(inputs=[scan(batch="A", weight="3103002500")], outputs=[]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [p["process_index"] for p in body["processes"]] == [0, 1]
    (batch,) = body["batches"]
    assert batch["opening"] == 1.0
    assert batch["produced"] == 2.0
    assert batch["consumed"] == 2.5
    assert batch["remaining"] == 0.5


def test_process_without_inputs_is_a_source():
    resp = genealogy(
        processes=[process(inputs=[], outputs=[scan(batch="P1")])],
    )
    assert resp.status_code == 200
    batch = resp.json()["batches"][0]
    assert batch["opening"] == 0.0
    assert batch["produced"] == 1.0
    assert batch["remaining"] == 1.0


def test_batches_are_listed_in_first_seen_order():
    resp = genealogy(
        opening_stock=[scan(batch="R2"), scan(batch="R1")],
        processes=[
            process(inputs=[], outputs=[scan(batch="P2"), scan(batch="P1")])
        ],
    )
    assert resp.status_code == 200
    assert [b["batch"] for b in resp.json()["batches"]] == ["R2", "R1", "P2", "P1"]


def test_digital_link_scans_are_accepted():
    resp = genealogy(
        opening_stock=["https://id.gs1.org/01/09501101530003/10/R1?3103=001000"],
        processes=[
            process(
                inputs=["https://id.gs1.org/01/09501101530003/10/R1?3103=001000"],
                outputs=[scan(batch="P1")],
            )
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["processes"][0]["inputs"][0]["batch"] == "R1"
    assert batches_by_name(body)["R1"]["remaining"] == 0.0


def test_ai_17_and_21_are_allowed_but_ignored():
    resp = genealogy(
        opening_stock=[f"01{GTIN_RAW}1725013110R1{GS}21SN1{GS}3103001000"],
    )
    assert resp.status_code == 200
    assert resp.json()["batches"][0]["opening"] == 1.0


def test_empty_request_is_200():
    resp = genealogy()
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "processes": [], "batches": []}


def test_response_shape():
    resp = genealogy(
        opening_stock=[scan()],
        processes=[process(inputs=[scan()], outputs=[scan(batch="P1")])],
    )
    body = resp.json()
    assert set(body) == {"target_unit", "processes", "batches"}
    assert set(body["processes"][0]) == {
        "process_index",
        "inputs",
        "outputs",
        "input_weight",
        "output_weight",
        "loss_weight",
    }
    assert set(body["processes"][0]["inputs"][0]) == {"gtin", "batch", "weight"}
    assert set(body["batches"][0]) == {
        "gtin",
        "batch",
        "opening",
        "produced",
        "consumed",
        "remaining",
    }


def test_weights_render_as_exact_six_place_numbers():
    resp = genealogy(
        opening_stock=[scan(weight="3103002513")],  # 2.513 kg
        processes=[process(inputs=[scan(weight="3103002513")], outputs=[])],
    )
    assert resp.status_code == 200
    assert '"opening":2.513000' in resp.text
    assert '"remaining":0.000000' in resp.text
    assert '"input_weight":2.513000' in resp.text
    assert '"loss_weight":2.513000' in resp.text
    assert '"opening":"2.513000"' not in resp.text


# ------------------------------------------------------- graph-level failures


def test_batch_produced_by_two_processes_is_422():
    resp = genealogy(
        processes=[
            process(inputs=[], outputs=[scan(batch="P1")]),
            process(inputs=[], outputs=[scan(batch="P1", weight="3103002000")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "MULTIPLE_PRODUCERS"
    assert "P1" in detail["error"]["message"]


def test_dependency_cycle_is_422():
    resp = genealogy(
        processes=[
            process(inputs=[scan(batch="B")], outputs=[scan(batch="A")]),
            process(inputs=[scan(batch="A")], outputs=[scan(batch="B")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["process_index"] == 0
    assert detail["cycle"] == [0, 1]


def test_process_consuming_its_own_output_is_a_cycle():
    resp = genealogy(
        processes=[process(inputs=[scan(batch="A")], outputs=[scan(batch="A")])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["process_index"] == 0
    assert detail["cycle"] == [0]


def test_longer_cycle_is_422():
    resp = genealogy(
        processes=[
            process(inputs=[], outputs=[scan(batch="OK")]),
            process(inputs=[scan(batch="C3")], outputs=[scan(batch="C1")]),
            process(inputs=[scan(batch="C1")], outputs=[scan(batch="C2")]),
            process(inputs=[scan(batch="C2")], outputs=[scan(batch="C3")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    # 1 consumes C3 (made by 3), 3 consumes C2 (made by 2), 2 consumes C1
    # (made by 1): the cycle is reported along the dependency edges.
    assert detail["process_index"] == 1
    assert detail["cycle"] == [1, 3, 2]


def test_consuming_a_batch_without_source_is_422():
    resp = genealogy(
        opening_stock=[scan(batch="A")],
        processes=[
            process(inputs=[], outputs=[]),
            process(inputs=[scan(batch="ZZ")], outputs=[scan(batch="P1")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["error"]["error"] == "UNKNOWN_BATCH"
    assert "ZZ" in detail["error"]["found"]


def test_overconsumption_of_opening_stock_is_422():
    resp = genealogy(
        opening_stock=[scan(weight="3103001000")],  # 1 kg available
        processes=[process(inputs=[scan(weight="3103002000")], outputs=[])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["error"]["error"] == "INSUFFICIENT_STOCK"
    assert "2.000000 kg" in detail["error"]["found"]
    assert "1.000000 kg" in detail["error"]["expected"]


def test_overconsumption_of_a_produced_batch_is_422():
    resp = genealogy(
        processes=[
            process(inputs=[], outputs=[scan(batch="P1", weight="3103001000")]),
            process(inputs=[scan(batch="P1", weight="3103003000")], outputs=[]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["error"]["error"] == "INSUFFICIENT_STOCK"


def test_consumption_is_checked_in_topological_not_request_order():
    # Process 0 (runs second) over-consumes P1; the failure names it, not
    # the first process in request order.
    resp = genealogy(
        processes=[
            process(inputs=[scan(batch="P1", weight="3103009000")], outputs=[]),
            process(inputs=[], outputs=[scan(batch="P1", weight="3103001000")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["error"]["error"] == "INSUFFICIENT_STOCK"


# ------------------------------------------------------- scan-level failures


def test_opening_stock_parse_failure_reports_scan_index():
    resp = genealogy(
        opening_stock=[scan(), f"010950110153000410B1{GS}3103001000"],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"
    assert set(detail) == {"scan_index", "error"}


def test_process_input_failure_reports_process_and_input_index():
    resp = genealogy(
        processes=[
            process(inputs=[], outputs=[]),
            process(inputs=[scan(), "bad"], outputs=[]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["input_index"] == 1
    assert detail["error"]["error"] == "INVALID_AI"
    assert set(detail) == {"process_index", "input_index", "error"}


def test_process_output_failure_reports_process_and_output_index():
    resp = genealogy(
        processes=[process(inputs=[], outputs=["010950110153000410B1"])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["output_index"] == 0
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"
    assert set(detail) == {"process_index", "output_index", "error"}


def test_missing_batch_is_422():
    resp = genealogy(opening_stock=[f"01{GTIN_RAW}3103001000"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 10" in detail["error"]["message"]


def test_missing_weight_is_422():
    resp = genealogy(opening_stock=[f"01{GTIN_RAW}10R1"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "MISSING_AI"
    assert "310n/320n" in detail["error"]["message"]


def test_duplicate_weight_is_422():
    resp = genealogy(
        opening_stock=[f"01{GTIN_RAW}10R1{GS}31030010003103002000"],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_duplicate_batch_is_422():
    resp = genealogy(
        opening_stock=[f"01{GTIN_RAW}10A{GS}10B{GS}3103001000"],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_missing_fields_are_422():
    assert client.post("/batch-genealogy", json={}).status_code == 422
    resp = client.post(
        "/batch-genealogy",
        json={"opening_stock": [], "processes": [{"inputs": []}]},
    )
    assert resp.status_code == 422


def test_invalid_target_unit_is_422():
    resp = genealogy(target_unit="g")
    assert resp.status_code == 422
