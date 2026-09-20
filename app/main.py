"""FastAPI application exposing the GS1 element-string parser."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .gs1 import GS1ParseError, parse
from .pickplan import DemandValidationError, pick_plan
from .recall import ItemConflictError, RuleValidationError, assess
from .summary import WeightSummaryError, summarize
from .traceaudit import EventConflictError, TraceAuditError, audit

app = FastAPI(
    title="GS1 Element String Parser",
    version="1.8.0",
    description=(
        "Parses GS1 input into structured, ordered AI elements. Accepts raw "
        "scan strings (FNC1 / ASCII 29 separated) and GS1 Digital Link URIs "
        "(http/https, AI/value pairs in path and query string). Supports AI "
        "01 (GTIN), 10 (batch/lot), 17 (expiry date), 21 (serial number) and "
        "the weight family 310n (net weight, kg) / 320n (net weight, lb), "
        "where n is 0-9 implied decimal places followed by a fixed 6-digit "
        "value; weight elements additionally carry decimal_value and unit. "
        "POST /weight-summary aggregates the net weights of many scans, "
        "grouped by GTIN and batch, using exact Decimal arithmetic. "
        "POST /weight-pick-plan aggregates scans by GTIN, batch and expiry "
        "and fulfils per-GTIN weight demands in FEFO order (earliest AI 17 "
        "expiry first), allowing a partial final allocation. Each request "
        "carries a planning date as_of and every demand a non-negative "
        "min_remaining_days (default 0): only stock whose actual expiry "
        "date is on or after as_of + min_remaining_days may serve that "
        "demand, and the weight ruled out by that threshold is reported "
        "per demand as excluded_weight. Every demand may instead set "
        'package_mode "whole" to treat each eligible scan as an indivisible '
        "package: the plan then picks the package subset reaching the "
        "demanded quantity with the smallest overage (reported as overage; "
        "shortage never goes negative), or every eligible package when no "
        "subset suffices. POST /recall-impact evaluates ordered recall "
        "rules over scanned physical items identified by GTIN + serial "
        "number (each scan carries AI 01, AI 17, AI 21 and one weight AI, "
        "AI 10 optional): identical re-scans of an item count once, "
        "re-scans conflicting on batch, expiry or weight are rejected with "
        "the current and first scan indices, and every item joins the "
        "first rule whose GTIN, optional batch set and optional inclusive "
        "expiry interval (YYMMDD, day 00 = last day of month) it matches. "
        "The response keeps the rule order and reports per-rule package "
        "counts and six-decimal half-even total weights, plus an aggregate "
        "of the items no rule matched. POST /trace-audit applies ordered "
        "traceability events (type receive/pick/ship plus a scan carrying "
        "AI 01, AI 21 and optionally AI 10/AI 17) to the per-item "
        "receive -> pick -> ship state machine: items are identified by "
        "GTIN + serial number, events that arrive out of order, repeat a "
        "completed stage, go back to an earlier stage or skip a stage are "
        "recorded as anomalies without moving the state, and events of one "
        "item conflicting on batch or expiry are rejected with the current "
        "and first event indices. The response keeps the event order and "
        "reports the state before/after each event plus its anomaly, and "
        "summarises the items by final state."
    ),
)


class ParseRequest(BaseModel):
    data: str = Field(
        ...,
        title="Raw scan string or GS1 Digital Link URI",
        description=(
            "Either a raw GS1 element string as scanned (variable-length "
            "fields separated by ASCII 29, encoded in JSON as , or a "
            "GS1 Digital Link URI beginning with http:// or https://. "
            "Digital Link values are percent-decoded as UTF-8; path segments "
            "must form AI/value pairs; each AI may appear only once across "
            "path and query string."
        ),
        examples=[
            "01095011015300031725013110ABC12321SN0001",
            "https://id.gs1.org/01/09501101530003/17/250131?10=ABC123&21=SN0001",
        ],
    )


class Element(BaseModel):
    ai: str = Field(description="Application identifier, e.g. '01'")
    name: str = Field(description="Short element name, e.g. 'GTIN'")
    value: str = Field(
        description="Field value without AI and separators (raw digits for "
        "310n/320n, before applying the implied decimal point)"
    )
    offset: int = Field(description="Byte offset of the AI within the input")
    decimal_value: float | None = Field(
        default=None,
        description=(
            "310n/320n only: the 6-digit value interpreted with n implied "
            "decimal places. Absent for all other AIs."
        ),
    )
    unit: str | None = Field(
        default=None,
        description='310n/320n only: measurement unit, "kg" (310n) or "lb" (320n). '
        "Absent for all other AIs.",
    )


class ParseResponse(BaseModel):
    elements: list[Element]


class WeightSummaryRequest(BaseModel):
    scans: list[str] = Field(
        ...,
        description=(
            "Raw GS1 scan strings and/or GS1 Digital Link URIs, one per item. "
            "Each must contain exactly one AI 01, at most one AI 10 and "
            "exactly one weight AI 310n/320n."
        ),
        examples=[
            [
                "010950110153000310B13103002513",
                "010950110153000310B13202002513",
            ]
        ],
    )
    target_unit: Literal["kg", "lb"] = Field(
        default="kg",
        description='Unit the group totals are converted to: "kg" or "lb" (default "kg").',
    )


class WeightGroup(BaseModel):
    """OpenAPI shape of one group; totals are rendered as exact JSON numbers."""

    gtin: str = Field(description="AI 01 GTIN-14 shared by the group")
    batch: str | None = Field(
        description="AI 10 batch/lot; null for scans that carried no batch"
    )
    count: int = Field(description="Number of scans aggregated into the group")
    unit: Literal["kg", "lb"] = Field(description="Unit of total (the target unit)")
    total: float = Field(
        description=(
            "Sum of net weights converted to the target unit, rounded to six "
            "decimal places with round-half-even. Rendered as a JSON number "
            "(e.g. 2.513000); computed internally with Decimal, never float."
        )
    )


class WeightSummaryResponse(BaseModel):
    target_unit: Literal["kg", "lb"]
    groups: list[WeightGroup]


class Demand(BaseModel):
    gtin: str = Field(
        ...,
        min_length=1,
        description="GTIN (AI 01 value) to satisfy; must be unique within one request.",
        examples=["09501101530003"],
    )
    quantity: Decimal = Field(
        ...,
        gt=0,
        description="Required net weight, expressed in target_unit.",
        examples=[3.5],
    )
    min_remaining_days: int = Field(
        default=0,
        description=(
            "Minimum remaining shelf life in days, counted from as_of. Only "
            "stock whose actual AI 17 expiry date is on or after "
            "as_of + min_remaining_days may serve this demand; the boundary "
            "date itself is eligible. Must be a non-negative integer; a "
            "negative value is rejected with 422 naming the demand_index."
        ),
        examples=[30],
    )
    package_mode: Literal["splittable", "whole"] = Field(
        default="splittable",
        description=(
            '"splittable" (default) allocates weight freely, allowing a '
            'partial final allocation. "whole" treats every eligible scan as '
            "an indivisible package: the plan picks the package subset "
            "reaching quantity with the smallest overage (ties broken by the "
            "lexicographically smallest FEFO index sequence), or every "
            "eligible package when no subset reaches quantity. A demand with "
            "more than 30 eligible packages is rejected with 422 naming the "
            "demand_index."
        ),
        examples=["whole"],
    )


class WeightPickPlanRequest(BaseModel):
    scans: list[str] = Field(
        ...,
        description=(
            "Raw GS1 scan strings and/or GS1 Digital Link URIs, one per item. "
            "Each must contain exactly one AI 01, exactly one AI 17 (expiry), "
            "at most one AI 10 and exactly one weight AI 310n/320n."
        ),
        examples=[
            [
                "01095011015300031725013110B1\x1d3103002000",
                "01095011015300031725022810B2\x1d3103002000",
            ]
        ],
    )
    target_unit: Literal["kg", "lb"] = Field(
        default="kg",
        description=(
            'Unit of the demand quantities and of every weight in the response: '
            '"kg" or "lb" (default "kg").'
        ),
    )
    as_of: date = Field(
        ...,
        title="Planning reference date",
        description=(
            "Planning date in YYYY-MM-DD form; must be a real calendar date "
            "(invalid values are rejected with 422). Per demand, stock is "
            "eligible only when its actual AI 17 expiry date -- day 00 "
            "meaning the last day of the month -- is on or after "
            "as_of + min_remaining_days."
        ),
        examples=["2025-01-15"],
    )
    demands: list[Demand] = Field(
        ...,
        description=(
            "One demand per GTIN; quantity is expressed in target_unit. "
            "A GTIN repeated across demands is rejected with 422."
        ),
        examples=[[{"gtin": "09501101530003", "quantity": 3}]],
    )

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of_is_yyyy_mm_dd(cls, value: Any) -> Any:
        """Accept only a YYYY-MM-DD string (or an already-parsed date)."""
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", value
        ):
            raise ValueError("as_of must be a date string in YYYY-MM-DD format")
        return value


class Allocation(BaseModel):
    """OpenAPI shape of one allocation; weight is rendered as an exact JSON number."""

    batch: str | None = Field(
        description="AI 10 batch/lot of the source group; null for scans without a batch"
    )
    expiry: str = Field(
        description="AI 17 value (YYMMDD) of the source group, exactly as scanned"
    )
    weight: float = Field(
        description=(
            "Net weight taken from this group, in the target unit, rounded to "
            "six decimal places with round-half-even. Computed internally with "
            "Decimal, never float."
        )
    )


class DemandPlan(BaseModel):
    """OpenAPI shape of one fulfilled demand; weights render as exact JSON numbers."""

    gtin: str = Field(description="GTIN of the demand")
    quantity: float = Field(
        description="Requested quantity, echoed in the target unit (six decimal places)"
    )
    fulfilled: float = Field(
        description="Total weight allocated to the demand (six decimal places)"
    )
    shortage: float = Field(
        description="quantity - fulfilled; 0.000000 when fully satisfied (six decimal places)"
    )
    overage: float = Field(
        description=(
            "fulfilled - quantity when a whole-package plan overshoots the "
            "demand; always 0.000000 for splittable demands (six decimal places)"
        )
    )
    excluded_weight: float = Field(
        description=(
            "Total weight of this GTIN's stock ruled out by the "
            "as_of + min_remaining_days threshold, in the target unit "
            "(six decimal places)"
        )
    )
    allocations: list[Allocation]


class WeightPickPlanResponse(BaseModel):
    target_unit: Literal["kg", "lb"]
    demands: list[DemandPlan]


class RecallRule(BaseModel):
    gtin: str = Field(
        ...,
        min_length=1,
        description=(
            "GTIN (AI 01 value) the rule applies to; compared literally, "
            "no check-digit validation."
        ),
        examples=["09501101530003"],
    )
    batches: list[str | None] | None = Field(
        default=None,
        description=(
            "Optional set of AI 10 batch/lot values: an item matches only "
            "when its batch is in the set (a null entry matches items "
            "scanned without AI 10). Omitted or null means any batch."
        ),
        examples=[["B1", "B2"]],
    )
    expiry_from: str | None = Field(
        default=None,
        description=(
            "Optional inclusive lower bound on the item's actual AI 17 "
            "expiry date, as a 6-digit YYMMDD string (day 00 = last day "
            "of the month)."
        ),
        examples=["250101"],
    )
    expiry_to: str | None = Field(
        default=None,
        description=(
            "Optional inclusive upper bound on the item's actual AI 17 "
            "expiry date, same YYMMDD form as expiry_from."
        ),
        examples=["250331"],
    )


class RecallImpactRequest(BaseModel):
    scans: list[str] = Field(
        ...,
        description=(
            "Raw GS1 scan strings and/or GS1 Digital Link URIs, one per "
            "physical item scan. Each must contain exactly one AI 01, "
            "exactly one AI 17 (expiry), exactly one AI 21 (serial), at "
            "most one AI 10 and exactly one weight AI 310n/320n."
        ),
        examples=[
            [
                "01095011015300031725013110B1\x1d21SN0001\x1d3103002000",
                "01095011015300031725022810B2\x1d21SN0002\x1d3202002513",
            ]
        ],
    )
    target_unit: Literal["kg", "lb"] = Field(
        default="kg",
        description='Unit of every total in the response: "kg" or "lb" (default "kg").',
    )
    rules: list[RecallRule] = Field(
        ...,
        description=(
            "Ordered recall rules; every physical item joins the first "
            "rule it matches, and the response keeps this order."
        ),
        examples=[[{"gtin": "09501101530003", "batches": ["B1"]}]],
    )


class RecallRuleResult(BaseModel):
    """OpenAPI shape of one rule's tally; totals render as exact JSON numbers."""

    rule_index: int = Field(description="Position of the rule in the request")
    gtin: str = Field(description="Echoed rule GTIN")
    batches: list[str | None] | None = Field(
        description="Echoed rule batch set; null when the rule has none"
    )
    expiry_from: str | None = Field(
        description="Echoed inclusive lower expiry bound (YYMMDD); null when absent"
    )
    expiry_to: str | None = Field(
        description="Echoed inclusive upper expiry bound (YYMMDD); null when absent"
    )
    package_count: int = Field(
        description="Number of distinct physical items claimed by this rule"
    )
    unit: Literal["kg", "lb"] = Field(description="Unit of total_weight (the target unit)")
    total_weight: float = Field(
        description=(
            "Sum of the claimed items' net weights in the target unit, "
            "rounded to six decimal places with round-half-even. Rendered "
            "as a JSON number (e.g. 2.513000); computed internally with "
            "Decimal, never float."
        )
    )


class UnmatchedItems(BaseModel):
    """OpenAPI shape of the aggregate of items no rule matched."""

    package_count: int = Field(description="Number of unmatched physical items")
    unit: Literal["kg", "lb"] = Field(description="Unit of total_weight (the target unit)")
    total_weight: float = Field(
        description="Sum of the unmatched items' net weights (six decimal places)"
    )


class RecallImpactResponse(BaseModel):
    target_unit: Literal["kg", "lb"]
    rules: list[RecallRuleResult]
    unmatched: UnmatchedItems


class TraceEvent(BaseModel):
    type: Literal["receive", "pick", "ship"] = Field(
        ...,
        description="Lifecycle stage the event records.",
        examples=["receive"],
    )
    scan: str = Field(
        ...,
        description=(
            "Raw GS1 scan string or GS1 Digital Link URI; must contain "
            "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), "
            "at most one AI 10 (batch/lot) and at most one AI 17 (expiry)."
        ),
        examples=["01095011015300031725013110B1\x1d21SN0001"],
    )


class TraceAuditRequest(BaseModel):
    events: list[TraceEvent] = Field(
        ...,
        description=(
            "Traceability events in occurrence order; each is applied to "
            "its item's receive -> pick -> ship state machine."
        ),
        examples=[
            [
                {
                    "type": "receive",
                    "scan": "01095011015300031725013110B1\x1d21SN0001",
                },
                {
                    "type": "pick",
                    "scan": "01095011015300031725013110B1\x1d21SN0001",
                },
            ]
        ],
    )


class TraceAnomaly(BaseModel):
    """Anomaly recorded on an event that did not advance the state machine."""

    error: str = Field(
        description=(
            "Anomaly code: FIRST_EVENT_NOT_RECEIVE, DUPLICATE_STAGE, "
            "STAGE_REGRESSION or SKIPPED_STAGE"
        )
    )
    message: str = Field(description="Human-readable description of the anomaly")
    expected: str = Field(description="The only event that would have advanced the state")
    found: str = Field(description="The event type actually received")


class TraceEventResult(BaseModel):
    event_index: int = Field(description="Position of the event in the request")
    type: Literal["receive", "pick", "ship"] = Field(description="Echoed event type")
    gtin: str = Field(description="AI 01 GTIN of the item the event refers to")
    serial: str = Field(description="AI 21 serial number of the item")
    state_before: Literal["none", "received", "picked", "shipped"] = Field(
        description="Item state before the event was processed"
    )
    state_after: Literal["none", "received", "picked", "shipped"] = Field(
        description="Item state after the event was processed"
    )
    anomaly: TraceAnomaly | None = Field(
        description="The recorded anomaly; null when the event advanced the state"
    )


class TraceStateSummary(BaseModel):
    """Item counts per final state; every key is always present."""

    none: int = Field(description="Items seen but never successfully received")
    received: int = Field(description="Items whose last completed stage is receive")
    picked: int = Field(description="Items whose last completed stage is pick")
    shipped: int = Field(description="Items whose last completed stage is ship")


class TraceAuditResponse(BaseModel):
    events: list[TraceEventResult]
    summary: TraceStateSummary


SIX_PLACES = Decimal("0.000001")


class _RawNumber(str):
    """Fixed-point numeric text, e.g. '2.513000', emitted verbatim as JSON."""


def _dump(value: Any) -> str:
    """JSON serializer that splices :class:`_RawNumber` tokens raw.

    The stdlib encoder stringifies Decimal and FastAPI's jsonable_encoder
    turns it into a float; totals must instead appear as exact six-place
    JSON numbers such as ``2.513000``, so they are pre-formatted and
    spliced in here as raw tokens.
    """
    if isinstance(value, _RawNumber):
        return str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_dump(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(str(k), ensure_ascii=False)}:{_dump(v)}"
                for k, v in value.items()
            )
            + "}"
        )
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _six(value: Decimal) -> _RawNumber:
    """Fixed-point token: ``value`` quantized half-even to six places."""
    return _RawNumber(
        format(value.quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN), "f")
    )


def _build_summary_response(result: dict) -> Response:
    """Quantize totals half-even to six places and render exact JSON."""
    payload = {
        "target_unit": result["target_unit"],
        "groups": [
            {
                "gtin": group["gtin"],
                "batch": group["batch"],
                "count": group["count"],
                "unit": group["unit"],
                "total": _six(group["total"]),
            }
            for group in result["groups"]
        ],
    }
    return Response(content=_dump(payload), media_type="application/json")


def _build_pick_plan_response(result: dict) -> Response:
    """Quantize every weight half-even to six places and render exact JSON."""
    payload = {
        "target_unit": result["target_unit"],
        "demands": [
            {
                "gtin": demand["gtin"],
                "quantity": _six(demand["quantity"]),
                "fulfilled": _six(demand["fulfilled"]),
                "shortage": _six(demand["shortage"]),
                "overage": _six(demand["overage"]),
                "excluded_weight": _six(demand["excluded_weight"]),
                "allocations": [
                    {
                        "batch": allocation["batch"],
                        "expiry": allocation["expiry"],
                        "weight": _six(allocation["weight"]),
                    }
                    for allocation in demand["allocations"]
                ],
            }
            for demand in result["demands"]
        ],
    }
    return Response(content=_dump(payload), media_type="application/json")


def _build_recall_response(result: dict) -> Response:
    """Quantize totals half-even to six places and render exact JSON."""
    payload = {
        "target_unit": result["target_unit"],
        "rules": [
            {
                "rule_index": rule["rule_index"],
                "gtin": rule["gtin"],
                "batches": rule["batches"],
                "expiry_from": rule["expiry_from"],
                "expiry_to": rule["expiry_to"],
                "package_count": rule["package_count"],
                "unit": rule["unit"],
                "total_weight": _six(rule["total_weight"]),
            }
            for rule in result["rules"]
        ],
        "unmatched": {
            "package_count": result["unmatched"]["package_count"],
            "unit": result["unmatched"]["unit"],
            "total_weight": _six(result["unmatched"]["total_weight"]),
        },
    }
    return Response(content=_dump(payload), media_type="application/json")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/parse", response_model=ParseResponse, response_model_exclude_none=True)
def parse_scan(req: ParseRequest) -> ParseResponse:
    try:
        elements = parse(req.data)
    except GS1ParseError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    return ParseResponse(elements=[Element(**vars(e)) for e in elements])


@app.post(
    "/weight-summary",
    response_model=WeightSummaryResponse,
)
def weight_summary(req: WeightSummaryRequest) -> Response:
    try:
        result = summarize(req.scans, req.target_unit)
    except WeightSummaryError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _build_summary_response(result)


@app.post(
    "/weight-pick-plan",
    response_model=WeightPickPlanResponse,
)
def weight_pick_plan(req: WeightPickPlanRequest) -> Response:
    try:
        result = pick_plan(
            req.scans,
            req.target_unit,
            [
                (d.gtin, d.quantity, d.min_remaining_days, d.package_mode)
                for d in req.demands
            ],
            req.as_of,
        )
    except (WeightSummaryError, DemandValidationError) as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _build_pick_plan_response(result)


@app.post(
    "/recall-impact",
    response_model=RecallImpactResponse,
)
def recall_impact(req: RecallImpactRequest) -> Response:
    try:
        result = assess(
            req.scans,
            req.target_unit,
            [
                (r.gtin, r.batches, r.expiry_from, r.expiry_to)
                for r in req.rules
            ],
        )
    except (WeightSummaryError, RuleValidationError, ItemConflictError) as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _build_recall_response(result)


@app.post(
    "/trace-audit",
    response_model=TraceAuditResponse,
)
def trace_audit(req: TraceAuditRequest) -> TraceAuditResponse:
    try:
        result = audit([(event.type, event.scan) for event in req.events])
    except (TraceAuditError, EventConflictError) as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return TraceAuditResponse(**result)
