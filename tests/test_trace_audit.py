"""End-to-end tests for the /trace-audit endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit
GTIN2 = "00000000000017"  # second valid GTIN


def audit(events):
    return client.post("/trace-audit", json={"events": events})


def scan(gtin=GTIN_OK, serial="SN1", batch=None, expiry=None, extra=""):
    """Build an element-string scan with 01/21, optional 10/17 and extras."""
    parts = [f"01{gtin}"]
    if expiry is not None:
        parts.append(f"17{expiry}")
    if batch is not None:
        parts.append(f"10{batch}{GS}")
    if extra:
        parts.append(extra)
    parts.append(f"21{serial}")
    return "".join(parts)


def event(type_, **kwargs):
    return {"type": type_, "scan": scan(**kwargs)}


# ---------------------------------------------------------------- happy path


def test_full_lifecycle_receive_pick_ship():
    resp = audit(
        [
            event("receive", batch="B1", expiry="250131"),
            event("pick", batch="B1", expiry="250131"),
            event("ship", batch="B1", expiry="250131"),
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [(e["state_before"], e["state_after"]) for e in body["events"]] == [
        ("none", "received"),
        ("received", "picked"),
        ("picked", "shipped"),
    ]
    assert [e["anomaly"] for e in body["events"]] == [None, None, None]
    assert body["summary"] == {"none": 0, "received": 0, "picked": 0, "shipped": 1}


def test_items_are_tracked_independently():
    resp = audit(
        [
            event("receive", serial="SN1"),
            event("receive", serial="SN2"),
            event("pick", serial="SN1"),
            event("receive", gtin=GTIN2, serial="SN1"),  # same serial, other GTIN
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [e["state_after"] for e in body["events"]] == [
        "received",
        "received",
        "picked",
        "received",
    ]
    assert all(e["anomaly"] is None for e in body["events"])
    assert body["summary"] == {"none": 0, "received": 2, "picked": 1, "shipped": 0}


def test_digital_link_scan_accepted():
    resp = audit(
        [
            {
                "type": "receive",
                "scan": "https://id.gs1.org/01/09501101530003/17/250131/21/SN7?10=B1",
            },
            {"type": "pick", "scan": scan(serial="SN7", batch="B1", expiry="250131")},
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [e["state_after"] for e in body["events"]] == ["received", "picked"]
    assert body["events"][0]["gtin"] == GTIN_OK
    assert body["events"][0]["serial"] == "SN7"


def test_weight_ai_is_ignored():
    resp = audit([event("receive", extra="3103002000")])
    assert resp.status_code == 200
    assert resp.json()["events"][0]["state_after"] == "received"


def test_optional_ais_may_be_absent():
    resp = audit([event("receive"), event("pick"), event("ship")])
    assert resp.status_code == 200
    assert resp.json()["summary"]["shipped"] == 1


def test_empty_events_give_zero_summary():
    resp = audit([])
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["summary"] == {"none": 0, "received": 0, "picked": 0, "shipped": 0}


def test_response_shape():
    resp = audit([event("receive", serial="SN1", batch="B1")])
    body = resp.json()
    assert set(body) == {"events", "summary"}
    assert set(body["events"][0]) == {
        "event_index",
        "type",
        "gtin",
        "serial",
        "state_before",
        "state_after",
        "anomaly",
    }
    assert body["events"][0]["event_index"] == 0
    assert body["events"][0]["type"] == "receive"
    assert set(body["summary"]) == {"none", "received", "picked", "shipped"}


# ------------------------------------------------------------------ anomalies


def test_first_event_pick_is_an_anomaly_and_does_not_advance():
    resp = audit([event("pick")])
    assert resp.status_code == 200
    (e,) = resp.json()["events"]
    assert e["state_before"] == "none"
    assert e["state_after"] == "none"
    assert e["anomaly"]["error"] == "FIRST_EVENT_NOT_RECEIVE"
    assert resp.json()["summary"]["none"] == 1


def test_first_event_ship_is_an_anomaly():
    resp = audit([event("ship")])
    (e,) = resp.json()["events"]
    assert e["state_after"] == "none"
    assert e["anomaly"]["error"] == "FIRST_EVENT_NOT_RECEIVE"


def test_item_recovers_when_receive_follows_the_anomaly():
    resp = audit([event("pick"), event("receive"), event("pick")])
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert [e["state_after"] for e in events] == ["none", "received", "picked"]
    assert [e["anomaly"] is None for e in events] == [False, True, True]


def test_repeated_non_receive_first_events_all_anomalous():
    resp = audit([event("pick"), event("ship"), event("pick")])
    events = resp.json()["events"]
    assert [e["anomaly"]["error"] for e in events] == [
        "FIRST_EVENT_NOT_RECEIVE"
    ] * 3
    assert resp.json()["summary"]["none"] == 1


def test_duplicate_receive_is_an_anomaly():
    resp = audit([event("receive"), event("receive")])
    events = resp.json()["events"]
    assert events[1]["state_before"] == "received"
    assert events[1]["state_after"] == "received"
    assert events[1]["anomaly"]["error"] == "DUPLICATE_STAGE"


def test_duplicate_pick_and_ship_are_anomalies():
    resp = audit(
        [
            event("receive"),
            event("pick"),
            event("pick"),  # duplicate
            event("ship"),
            event("ship"),  # duplicate
        ]
    )
    events = resp.json()["events"]
    assert events[2]["anomaly"]["error"] == "DUPLICATE_STAGE"
    assert events[2]["state_after"] == "picked"
    assert events[4]["anomaly"]["error"] == "DUPLICATE_STAGE"
    assert events[4]["state_after"] == "shipped"
    assert resp.json()["summary"]["shipped"] == 1


def test_regression_to_an_earlier_stage_is_an_anomaly():
    resp = audit(
        [
            event("receive"),
            event("pick"),
            event("receive"),  # regression
            event("ship"),
            event("pick"),  # regression
        ]
    )
    events = resp.json()["events"]
    assert events[2]["anomaly"]["error"] == "STAGE_REGRESSION"
    assert events[2]["state_after"] == "picked"
    assert events[4]["anomaly"]["error"] == "STAGE_REGRESSION"
    assert events[4]["state_after"] == "shipped"


def test_ship_before_pick_skips_a_stage():
    resp = audit([event("receive"), event("ship")])
    events = resp.json()["events"]
    assert events[1]["anomaly"]["error"] == "SKIPPED_STAGE"
    assert events[1]["state_after"] == "received"
    # The item can still be picked and shipped afterwards.
    resp = audit(
        [event("receive"), event("ship"), event("pick"), event("ship")]
    )
    events = resp.json()["events"]
    assert [e["state_after"] for e in events] == [
        "received",
        "received",
        "picked",
        "shipped",
    ]


def test_anomaly_carries_expected_and_found():
    resp = audit([event("receive"), event("receive")])
    anomaly = resp.json()["events"][1]["anomaly"]
    assert set(anomaly) == {"error", "message", "expected", "found"}
    assert anomaly["expected"] == "the next event 'pick'"
    assert anomaly["found"] == "'receive'"


def test_terminal_state_reports_no_further_event():
    resp = audit(
        [event("receive"), event("pick"), event("ship"), event("receive")]
    )
    anomaly = resp.json()["events"][3]["anomaly"]
    assert anomaly["error"] == "STAGE_REGRESSION"
    assert "already shipped" in anomaly["expected"]


def test_summary_counts_each_final_state():
    resp = audit(
        [
            event("receive", serial="SN1"),
            event("pick", serial="SN1"),
            event("ship", serial="SN1"),  # SN1 -> shipped
            event("receive", serial="SN2"),
            event("pick", serial="SN2"),  # SN2 -> picked
            event("receive", serial="SN3"),  # SN3 -> received
            event("ship", serial="SN4"),  # SN4 -> none (anomaly)
        ]
    )
    assert resp.status_code == 200
    assert resp.json()["summary"] == {
        "none": 1,
        "received": 1,
        "picked": 1,
        "shipped": 1,
    }


# ------------------------------------------------------------- 422 conflicts


def test_conflicting_batch_returns_422_with_both_indices():
    resp = audit(
        [
            event("receive", serial="SN1", batch="B1"),
            event("receive", serial="SN2", batch="B2"),  # other item in between
            event("pick", serial="SN1", batch="B9"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["event_index"] == 2
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "CONFLICTING_BATCH"
    assert "B1" in detail["error"]["expected"]
    assert "B9" in detail["error"]["found"]


def test_conflicting_expiry_returns_422():
    resp = audit(
        [
            event("receive", expiry="250131"),
            event("pick", expiry="250228"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["event_index"] == 1
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "CONFLICTING_EXPIRY"


def test_batch_absent_then_present_is_a_conflict():
    resp = audit([event("receive"), event("pick", batch="B1")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CONFLICTING_BATCH"
    assert "no batch" in detail["error"]["expected"]


def test_expiry_present_then_absent_is_a_conflict():
    resp = audit([event("receive", expiry="250131"), event("pick")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CONFLICTING_EXPIRY"
    assert "no expiry" in detail["error"]["found"]


def test_same_attributes_in_different_scans_are_consistent():
    # Element string and Digital Link forms of one item agree: no conflict.
    resp = audit(
        [
            event("receive", batch="B1", expiry="250131"),
            {
                "type": "pick",
                "scan": "https://id.gs1.org/01/09501101530003/17/250131/21/SN1?10=B1",
            },
        ]
    )
    assert resp.status_code == 200
    assert [e["state_after"] for e in resp.json()["events"]] == [
        "received",
        "picked",
    ]


def test_conflict_is_checked_even_after_anomalous_first_event():
    # The anomalous first pick still defines the item's attributes.
    resp = audit(
        [
            event("pick", batch="B1"),
            event("receive", batch="B2"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["event_index"] == 1
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "CONFLICTING_BATCH"


# ------------------------------------------------------- scan-level failures


def test_parse_failure_reports_event_index():
    resp = audit(
        [
            event("receive"),
            {"type": "pick", "scan": "01095011015300041725013121SN1"},  # bad digit
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["event_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"
    assert set(detail) == {"event_index", "error"}


def test_missing_serial_is_422():
    resp = audit([{"type": "receive", "scan": f"01{GTIN_OK}17250131"}])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["event_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "21" in detail["error"]["message"]


def test_missing_gtin_is_422():
    resp = audit([{"type": "receive", "scan": "1725013121SN1"}])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "MISSING_AI"
    assert "01" in detail["error"]["message"]


def test_duplicate_serial_is_422():
    resp = audit(
        [{"type": "receive", "scan": f"01{GTIN_OK}21A{GS}21B"}]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_duplicate_batch_is_422():
    resp = audit(
        [{"type": "receive", "scan": f"01{GTIN_OK}10A{GS}10B{GS}21SN1"}]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_duplicate_expiry_is_422():
    resp = audit(
        [{"type": "receive", "scan": f"01{GTIN_OK}172501311725022821SN1"}]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_unknown_event_type_is_422():
    resp = audit([{"type": "return", "scan": scan()}])
    assert resp.status_code == 422


def test_missing_scan_field_is_422():
    resp = client.post("/trace-audit", json={"events": [{"type": "receive"}]})
    assert resp.status_code == 422


def test_missing_events_field_is_422():
    resp = client.post("/trace-audit", json={})
    assert resp.status_code == 422
