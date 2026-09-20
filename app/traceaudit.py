"""Traceability event audit over parsed GS1 scans.

Each event carries a ``type`` (``receive`` / ``pick`` / ``ship``) and a
``scan`` fed through :func:`app.gs1.parse`; the scan must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 21 (serial number),
* zero or one AI 10 (batch/lot) -- optional,
* zero or one AI 17 (expiry date) -- optional.

Weight elements of the 310n/320n family are ignored when present.  Any
parser failure or missing/duplicated required AI fails the whole request
with :class:`TraceAuditError` whose ``detail`` names the offending
``event_index`` -- the same 422 shape as ``/weight-summary`` & friends.

Physical items are identified by (GTIN, serial).  The batch (AI 10 raw
value, absence included) and expiry (AI 17 raw value, absence included)
of an item are fixed by its first event; a later event that disagrees
fails the whole request with :class:`EventConflictError` whose
``detail`` carries ``event_index`` (the conflicting event) and
``first_index`` (the event that first defined the item) -- mirroring
the re-scan conflicts of ``/recall-impact``.

Every item walks the receive -> pick -> ship state machine through the
states ``none`` (no stage completed yet), ``received``, ``picked`` and
``shipped``; only the event matching the item's next stage advances it.
Anything else is recorded on the event as an anomaly and the state does
not move:

* ``FIRST_EVENT_NOT_RECEIVE`` -- a pick/ship event reached an item that
  has not been received yet;
* ``DUPLICATE_STAGE`` -- the event repeats the stage the item has just
  completed (e.g. a second ``receive``);
* ``STAGE_REGRESSION`` -- the event goes back to an earlier stage
  (e.g. ``receive`` after ``pick``);
* ``SKIPPED_STAGE`` -- the event jumps past an uncompleted stage
  (``ship`` before ``pick``).

The result keeps the input order: every event reports the item's state
before and after it was processed plus the anomaly (``None`` when the
event advanced the machine), and ``summary`` counts the distinct items
by their final state.
"""

from __future__ import annotations

from .gs1 import GS1ParseError, Element, parse
from .summary import ScanValidationError

EVENT_TYPES = ("receive", "pick", "ship")
STATES = ("none", "received", "picked", "shipped")

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), "
    "at most one AI 10 (batch/lot) and at most one AI 17 (expiry date)"
)

# Stage completed by each event type; states mirror the last completed
# stage, with "none" = no stage completed yet.
_STAGE_OF_EVENT = {"receive": 0, "pick": 1, "ship": 2}
_STAGE_OF_STATE = {"none": -1, "received": 0, "picked": 1, "shipped": 2}
_NEXT_EVENT = {"none": "receive", "received": "pick", "picked": "ship", "shipped": None}
_STATE_AFTER = {"receive": "received", "pick": "picked", "ship": "shipped"}


class TraceAuditError(Exception):
    """A scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: ``event_index`` plus the
    original parser/validation error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class EventConflictError(Exception):
    """Two events of one physical item disagree on batch or expiry.

    ``detail`` is the complete 422 payload: ``event_index`` (the
    conflicting event) and ``first_index`` (the event that first
    defined the item) plus the conflict under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _failure(index: int, error: GS1ParseError) -> TraceAuditError:
    """Wrap a parser/validation error with the offending event index."""
    return TraceAuditError({"event_index": index, "error": error.to_dict()})


def _extract(
    index: int, elements: list[Element]
) -> tuple[str, str, str | None, str | None]:
    """Pull (GTIN, serial, batch or None, expiry or None) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
    serials = [e for e in elements if e.ai == "21"]
    batches = [e for e in elements if e.ai == "10"]
    expiries = [e for e in elements if e.ai == "17"]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"event {index}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"event {index}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(serials) != 1:
        if not serials:
            raise ScanValidationError(
                "MISSING_AI",
                f"event {index}: missing required AI 21 (serial number)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"event {index}: AI 21 (serial number) appears {len(serials)} times; exactly one is required",
            expected="exactly one AI 21 (serial number)",
            found=f"{len(serials)} AI 21 elements",
            offset=serials[1].offset,
        )

    if len(batches) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"event {index}: AI 10 (batch/lot) appears {len(batches)} times; at most one is allowed",
            expected="at most one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(expiries) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"event {index}: AI 17 (expiry date) appears {len(expiries)} times; at most one is allowed",
            expected="at most one AI 17 (expiry date)",
            found=f"{len(expiries)} AI 17 elements",
            offset=expiries[1].offset,
        )

    return (
        gtins[0].value,
        serials[0].value,
        batches[0].value if batches else None,
        expiries[0].value if expiries else None,
    )


def _batch_text(batch: str | None) -> str:
    return f"batch {batch!r}" if batch is not None else "no batch (AI 10 absent)"


def _expiry_text(expiry: str | None) -> str:
    return f"expiry {expiry!r}" if expiry is not None else "no expiry (AI 17 absent)"


def _conflict(index: int, first: dict, code: str, first_text: str, current_text: str):
    first_index = first["index"]
    raise EventConflictError(
        {
            "event_index": index,
            "first_index": first_index,
            "error": {
                "error": code,
                "message": (
                    f"event {index}: item GTIN '{first['gtin']}' serial "
                    f"'{first['serial']}' was first seen at event "
                    f"{first_index} with {first_text}, but this event has "
                    f"{current_text}"
                ),
                "expected": f"{first_text} (event {first_index})",
                "found": current_text,
            },
        }
    )


def _check_consistent(
    index: int, first: dict, batch: str | None, expiry: str | None
) -> None:
    """Reject an event of ``first``'s item that disagrees on an attribute."""
    if batch != first["batch"]:
        _conflict(
            index,
            first,
            "CONFLICTING_BATCH",
            _batch_text(first["batch"]),
            _batch_text(batch),
        )
    if expiry != first["expiry"]:
        _conflict(
            index,
            first,
            "CONFLICTING_EXPIRY",
            _expiry_text(first["expiry"]),
            _expiry_text(expiry),
        )


def _expected_text(state: str) -> str:
    """Human text of the only event that would advance ``state``."""
    nxt = _NEXT_EVENT[state]
    if nxt is None:
        return "no further event (the item is already shipped)"
    return f"the next event '{nxt}'"


def _step(index: int, item: dict, event_type: str) -> dict | None:
    """Advance ``item``'s state machine for one event.

    Returns the anomaly dict, or ``None`` when the event was the item's
    expected next stage and the state advanced.  An anomalous event
    never moves the state.
    """
    state = item["state"]
    if event_type == _NEXT_EVENT[state]:
        item["state"] = _STATE_AFTER[event_type]
        return None

    gtin, serial = item["gtin"], item["serial"]
    if state == "none":
        return {
            "error": "FIRST_EVENT_NOT_RECEIVE",
            "message": (
                f"event {index}: item GTIN '{gtin}' serial '{serial}' "
                f"has not been received, but got a '{event_type}' event; "
                "the first event of an item must be 'receive'"
            ),
            "expected": _expected_text(state),
            "found": f"'{event_type}'",
        }

    event_stage = _STAGE_OF_EVENT[event_type]
    item_stage = _STAGE_OF_STATE[state]
    if event_stage == item_stage:
        return {
            "error": "DUPLICATE_STAGE",
            "message": (
                f"event {index}: item GTIN '{gtin}' serial '{serial}' "
                f"is already '{state}'; the '{event_type}' stage was "
                "already completed"
            ),
            "expected": _expected_text(state),
            "found": f"'{event_type}'",
        }
    if event_stage < item_stage:
        return {
            "error": "STAGE_REGRESSION",
            "message": (
                f"event {index}: item GTIN '{gtin}' serial '{serial}' "
                f"is already '{state}'; a '{event_type}' event goes "
                "back to an earlier stage"
            ),
            "expected": _expected_text(state),
            "found": f"'{event_type}'",
        }
    return {
        "error": "SKIPPED_STAGE",
        "message": (
            f"event {index}: item GTIN '{gtin}' serial '{serial}' "
            f"is '{state}'; a '{event_type}' event skips the "
            f"'{_NEXT_EVENT[state]}' stage"
        ),
        "expected": _expected_text(state),
        "found": f"'{event_type}'",
    }


def audit(events: list[tuple[str, str]]) -> dict:
    """Run every (type, scan) event through the items' state machines.

    ``events`` is a list of ``(type, scan)`` pairs in occurrence order
    with ``type`` one of :data:`EVENT_TYPES`.  Returns ``{"events":
    [...], "summary": {...}}`` with one entry per event in input order
    -- ``event_index``, the echoed ``type``, the item's ``gtin`` and
    ``serial``, ``state_before`` / ``state_after`` and the ``anomaly``
    (``None`` when the event advanced the machine) -- and ``summary``
    counting the distinct items by final state, with every key of
    :data:`STATES` always present.
    """
    items: dict[tuple[str, str], dict] = {}
    results = []
    for index, (event_type, scan) in enumerate(events):
        try:
            elements = parse(scan)
        except GS1ParseError as exc:
            raise _failure(index, exc) from exc
        try:
            gtin, serial, batch, expiry = _extract(index, elements)
        except ScanValidationError as exc:
            raise _failure(index, exc) from exc

        key = (gtin, serial)
        item = items.get(key)
        if item is None:
            item = items[key] = {
                "index": index,
                "gtin": gtin,
                "serial": serial,
                "batch": batch,
                "expiry": expiry,
                "state": "none",
            }
        else:
            _check_consistent(index, item, batch, expiry)

        state_before = item["state"]
        anomaly = _step(index, item, event_type)
        results.append(
            {
                "event_index": index,
                "type": event_type,
                "gtin": gtin,
                "serial": serial,
                "state_before": state_before,
                "state_after": item["state"],
                "anomaly": anomaly,
            }
        )

    summary = {state: 0 for state in STATES}
    for item in items.values():
        summary[item["state"]] += 1
    return {"events": results, "summary": summary}
