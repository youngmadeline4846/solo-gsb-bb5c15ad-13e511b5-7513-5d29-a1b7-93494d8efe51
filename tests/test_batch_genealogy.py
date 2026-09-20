"""End-to-end tests for the /batch-genealogy endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit
GTIN2 = "00000000000017"  # second valid GTIN


def genealogy(opening_stock, processes, target_unit=None):
    body = {"opening_stock": opening_stock, "processes": processes}
    if target_unit is not None:
        body["target_unit"] = target_unit
    return client.post("/batch-genealogy", json=body)


def scan(gtin=GTIN_OK, batch="B1", weight="3103002000"):
    """Build an element-string scan with 01/10 and one weight AI."""
    return f"01{gtin}10{batch}{GS}{weight}"


def step(inputs, outputs):
    return {"inputs": inputs, "outputs": outputs}


def remaining(body):
    return {b["batch"]: b["remaining"] for b in body["batches"]}


# ---------------------------------------------------------------- happy path


def test_processes_run_in_stable_topological_order():
    # Process 0 is listed first but consumes what process 1 produces.
    resp = genealogy(
        [scan(batch="RAW", weight="3103010000")],  # 10 kg
        [
            step(
                [scan(batch="MID", weight="3103004000")],
                [scan(batch="FIN", weight="3103003800")],
            ),
            step(
                [scan(batch="RAW", weight="3103005000")],
                [scan(batch="MID", weight="3103004000")],
            ),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [p["process_index"] for p in body["processes"]] == [1, 0]

    first, second = body["processes"]
    assert first["input_weight"] == 5.0
    assert first["output_weight"] == 4.0
    assert first["loss_weight"] == 1.0
    assert second["input_weight"] == 4.0
    assert second["output_weight"] == 3.8
    assert second["loss_weight"] == 0.2

    assert body["batches"] == [
        {"gtin": GTIN_OK, "batch": "RAW", "remaining": 5.0},
        {"gtin": GTIN_OK, "batch": "MID", "remaining": 0.0},
        {"gtin": GTIN_OK, "batch": "FIN", "remaining": 3.8},
    ]


def test_independent_processes_keep_request_order():
    resp = genealogy(
        [
            scan(batch="A", weight="3103001000"),
            scan(batch="B", weight="3103001000"),
        ],
        [
            step([scan(batch="B", weight="3103001000")], []),
            step([scan(batch="A", weight="3103001000")], []),
        ],
    )
    assert resp.status_code == 200
    assert [p["process_index"] for p in resp.json()["processes"]] == [0, 1]


def test_repeated_scans_of_one_batch_are_aggregated():
    resp = genealogy(
        [
            scan(batch="RAW", weight="3103002000"),
            scan(batch="RAW", weight="3103003000"),
        ],
        [step([scan(batch="RAW", weight="3103005000")], [])],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["batches"] == [
        {"gtin": GTIN_OK, "batch": "RAW", "remaining": 0.0}
    ]
    assert body["processes"][0]["loss_weight"] == 5.0


def test_weights_are_converted_to_the_target_unit():
    # Opening 25.13 lb = 11.3987762581 kg; the process consumes 5 kg.
    resp = genealogy(
        [scan(batch="RAW", weight="3202002513")],
        [
            step(
                [scan(batch="RAW", weight="3103005000")],
                [scan(batch="FIN", weight="3103006000")],
            )
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert remaining(body) == {"RAW": 6.398776, "FIN": 6.0}
    # Output exceeds input: the loss goes negative.
    assert body["processes"][0]["loss_weight"] == -1.0


def test_target_unit_lb():
    resp = genealogy(
        [scan(batch="RAW")],  # 2 kg
        [],
        target_unit="lb",
    )
    assert resp.status_code == 200
    # 2 kg = 4.409245243... lb, six places half-even.
    assert resp.json()["batches"][0]["remaining"] == 4.409245
    assert resp.json()["target_unit"] == "lb"


def test_produced_quantity_adds_to_opening_stock_of_the_same_batch():
    resp = genealogy(
        [scan(batch="X", weight="3103005000")],  # 5 kg
        [
            step([], [scan(batch="X", weight="3103003000")]),  # +3 kg
            step([scan(batch="X", weight="3103006000")], []),  # -6 kg
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    # The consumer must run after the producer even though it could have
    # been served from the opening stock alone.
    assert [p["process_index"] for p in body["processes"]] == [0, 1]
    assert remaining(body) == {"X": 2.0}


def test_batches_are_identified_by_gtin_and_batch():
    resp = genealogy(
        [
            scan(batch="B1", weight="3103001000"),
            scan(gtin=GTIN2, batch="B1", weight="3103002000"),
        ],
        [step([scan(gtin=GTIN2, batch="B1", weight="3103002000")], [])],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["batches"] == [
        {"gtin": GTIN_OK, "batch": "B1", "remaining": 1.0},
        {"gtin": GTIN2, "batch": "B1", "remaining": 0.0},
    ]


def test_digital_link_scans_are_accepted():
    resp = genealogy(
        ["https://id.gs1.org/01/09501101530003/10/RAW?3103=010000"],
        [
            step(
                ["https://id.gs1.org/01/09501101530003/10/RAW?3103=005000"],
                [scan(batch="FIN", weight="3103004000")],
            )
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert remaining(body) == {"RAW": 5.0, "FIN": 4.0}


def test_expiry_and_serial_ais_are_ignored():
    resp = genealogy(
        [f"01{GTIN_OK}1725013110RAW{GS}21SN1{GS}3103010000"],
        [step([scan(batch="RAW", weight="3103005000")], [])],
    )
    assert resp.status_code == 200
    assert remaining(resp.json()) == {"RAW": 5.0}


def test_empty_request_returns_empty_genealogy():
    resp = genealogy([], [])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "processes": [], "batches": []}


def test_response_shape():
    resp = genealogy(
        [scan(batch="RAW")],
        [step([scan(batch="RAW")], [scan(batch="FIN", weight="3103001000")])],
    )
    body = resp.json()
    assert set(body) == {"target_unit", "processes", "batches"}
    proc = body["processes"][0]
    assert set(proc) == {
        "process_index",
        "inputs",
        "outputs",
        "input_weight",
        "output_weight",
        "loss_weight",
    }
    assert set(proc["inputs"][0]) == {"gtin", "batch", "weight"}
    assert set(proc["outputs"][0]) == {"gtin", "batch", "weight"}
    assert set(body["batches"][0]) == {"gtin", "batch", "remaining"}


# ------------------------------------------------------------- graph errors


def test_batch_produced_by_two_processes_is_422():
    resp = genealogy(
        [],
        [
            step([], [scan(batch="X")]),
            step([], [scan(batch="X", weight="3103001000")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "MULTIPLE_PRODUCERS"


def test_input_batch_without_source_is_422():
    resp = genealogy(
        [],
        [step([scan(batch="GHOST")], [])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["error"]["error"] == "UNKNOWN_BATCH"
    assert "GHOST" in detail["error"]["message"]


def test_dependency_cycle_is_422():
    resp = genealogy(
        [],
        [
            step([scan(batch="A")], [scan(batch="B")]),
            step([scan(batch="B")], [scan(batch="A")]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["error"]["error"] == "PROCESS_CYCLE"


def test_self_consumption_is_a_cycle():
    resp = genealogy(
        [],
        [step([scan(batch="A")], [scan(batch="A", weight="3103001000")])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["error"]["error"] == "PROCESS_CYCLE"


def test_graph_errors_are_reported_before_stock_errors():
    # Processes 0/1 form a cycle; process 2 would over-consume: the
    # graph-stage error wins because no stock moves before it passes.
    resp = genealogy(
        [scan(batch="S", weight="3103001000")],
        [
            step([scan(batch="A")], [scan(batch="B")]),
            step([scan(batch="B")], [scan(batch="A")]),
            step([scan(batch="S", weight="3103005000")], []),
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "PROCESS_CYCLE"


# ------------------------------------------------------------ stock errors


def test_over_consumption_of_opening_stock_is_422():
    resp = genealogy(
        [scan(batch="RAW", weight="3103003000")],  # 3 kg
        [step([scan(batch="RAW", weight="3103005000")], [])],  # needs 5 kg
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["error"]["error"] == "INSUFFICIENT_STOCK"
    assert "5.000000" in detail["error"]["found"]
    assert "3.000000" in detail["error"]["expected"]


def test_consumption_beyond_opening_plus_produced_is_422():
    resp = genealogy(
        [scan(batch="RAW", weight="3103005000")],  # 5 kg
        [
            step(
                [scan(batch="RAW", weight="3103005000")],
                [scan(batch="MID", weight="3103002000")],
            ),
            step([scan(batch="MID", weight="3103003000")], []),  # needs 3 > 2
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["error"]["error"] == "INSUFFICIENT_STOCK"


def test_competing_consumers_run_in_request_order():
    resp = genealogy(
        [scan(batch="RAW", weight="3103005000")],  # 5 kg
        [
            step([scan(batch="RAW", weight="3103004000")], []),  # 4 kg, 1 left
            step([scan(batch="RAW", weight="3103002000")], []),  # 2 kg > 1 kg
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 1
    assert detail["error"]["error"] == "INSUFFICIENT_STOCK"


# ------------------------------------------------------------- scan errors


def test_bad_opening_stock_scan_reports_stock_index():
    resp = genealogy(
        [scan(), "010950110153000410B1\x1d3103002000"],  # bad check digit
        [],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert set(detail) == {"stock_index", "error"}
    assert detail["stock_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_bad_process_input_reports_process_and_input_index():
    resp = genealogy(
        [scan(batch="RAW")],
        [step([scan(batch="RAW"), "junk"], [])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["input_index"] == 1
    assert detail["error"]["error"] == "INVALID_AI"


def test_bad_process_output_reports_process_and_output_index():
    resp = genealogy(
        [],
        [step([], ["010950110153000410B1\x1d3103002000"])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["process_index"] == 0
    assert detail["output_index"] == 0
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_missing_batch_is_422():
    resp = genealogy([f"01{GTIN_OK}3103002000"], [])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["stock_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "10" in detail["error"]["message"]


def test_missing_weight_is_422():
    resp = genealogy([f"01{GTIN_OK}10B1"], [])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "MISSING_AI"


def test_two_weight_elements_are_422():
    resp = genealogy([f"01{GTIN_OK}10B1{GS}31030020003202002513"], [])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_missing_required_fields_are_422():
    assert client.post("/batch-genealogy", json={}).status_code == 422
    resp = client.post(
        "/batch-genealogy",
        json={"opening_stock": [], "processes": [{"inputs": []}]},
    )
    assert resp.status_code == 422


def test_invalid_target_unit_is_422():
    resp = genealogy([], [], target_unit="g")
    assert resp.status_code == 422
