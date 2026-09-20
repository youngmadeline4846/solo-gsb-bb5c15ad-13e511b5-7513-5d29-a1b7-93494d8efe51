"""Recall-impact assessment of parsed GS1 scans against ordered rules.

Each scan is fed through :func:`app.gs1.parse` and must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 17 (expiry date; day ``00`` denotes the last day of the
  month, per the GS1 General Specifications),
* exactly one AI 21 (serial number),
* zero or one AI 10 (batch/lot) -- optional,
* exactly one weight element of the 310n (kg) / 320n (lb) family.

Any parser failure or missing/duplicated required AI fails the whole
request with :class:`app.summary.WeightSummaryError` whose ``detail``
names the offending scan index -- the same 422 shape as
``/weight-summary`` and ``/weight-pick-plan``.

Physical items are identified by (GTIN, serial).  A repeated scan of the
same item is counted once when every attribute agrees: batch (AI 10 raw
value, absence included), expiry (AI 17 raw value) and weight (same
unit with a numerically equal exact value -- ``3103 002513`` and
``3104 025130`` both encode 2.513 kg and are one measurement).  A
re-scan that disagrees on batch, expiry or weight fails the whole
request with :class:`ItemConflictError` whose ``detail`` carries
``scan_index`` (the conflicting re-scan) and ``first_index`` (the scan
that first defined the item).

Recall rules are evaluated in request order; each carries a required
``gtin``, an optional ``batches`` set and an optional inclusive expiry
interval ``expiry_from`` / ``expiry_to`` (AI 17 style YYMMDD, day ``00``
denoting the last day of the month; either bound may be omitted).  An
item matches a rule when the GTINs are equal, its batch is a member of
the set (a rule without ``batches`` accepts any batch; a ``None`` entry
matches items scanned without AI 10) and its actual expiry date lies
inside the interval.  Every item joins the **first** rule it matches;
items matched by no rule are aggregated under ``unmatched``.  Rules are
validated before any scan is parsed: a malformed or inverted bound fails
with :class:`RuleValidationError` naming the ``rule_index``.

All weights are rebuilt with :class:`decimal.Decimal` from the original
six digits and converted with the fixed factors of :mod:`app.summary`;
rounding to six places happens only when the response is rendered.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, _check_date, decode_weight, parse
from .pickplan import _expiry_date
from .summary import (
    ARITHMETIC_PRECISION,
    ScanValidationError,
    WeightSummaryError,
    _failure,
    _to_target,
)

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 17 (expiry date), "
    "exactly one AI 21 (serial number), at most one AI 10 (batch/lot), "
    "and exactly one weight AI 310n/320n"
)


class RuleValidationError(Exception):
    """A recall rule violated the request shape (e.g. an inverted interval).

    ``detail`` is the complete 422 payload: ``rule_index`` plus the
    validation error under ``error``, mirroring the demand-level shape
    of ``/weight-pick-plan``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class ItemConflictError(Exception):
    """Two scans share GTIN+serial but disagree on batch, expiry or weight.

    ``detail`` is the complete 422 payload: ``scan_index`` (the
    conflicting re-scan) and ``first_index`` (the scan that first
    defined the item) plus the conflict under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _extract(
    index: int, elements: list[Element]
) -> tuple[str, str | None, str, str, Decimal, str]:
    """Pull (GTIN, batch or None, AI 17, AI 21, exact weight, unit)."""
    gtins = [e for e in elements if e.ai == "01"]
    expiries = [e for e in elements if e.ai == "17"]
    serials = [e for e in elements if e.ai == "21"]
    batches = [e for e in elements if e.ai == "10"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(expiries) != 1:
        if not expiries:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required AI 17 (expiry date)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 17 (expiry date) appears {len(expiries)} times; exactly one is required",
            expected="exactly one AI 17 (expiry date)",
            found=f"{len(expiries)} AI 17 elements",
            offset=expiries[1].offset,
        )

    if len(serials) != 1:
        if not serials:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required AI 21 (serial number)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 21 (serial number) appears {len(serials)} times; exactly one is required",
            expected="exactly one AI 21 (serial number)",
            found=f"{len(serials)} AI 21 elements",
            offset=serials[1].offset,
        )

    if len(batches) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 10 (batch/lot) appears {len(batches)} times; at most one is allowed",
            expected="at most one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return (
        gtins[0].value,
        batches[0].value if batches else None,
        expiries[0].value,
        serials[0].value,
        value,
        weight.unit,
    )


def _bound_date(rule_index: int, field: str, value) -> date:
    """Actual date of a YYMMDD rule bound (day ``00`` = last day of month)."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}", value):
        raise RuleValidationError(
            {
                "rule_index": rule_index,
                "error": {
                    "error": "INVALID_EXPIRY_BOUND",
                    "message": (
                        f"rule {rule_index}: {field} must be a 6-digit "
                        f"YYMMDD date, got {value!r}"
                    ),
                    "expected": "6-digit YYMMDD date (day 00 = last day of month)",
                    "found": str(value),
                },
            }
        )
    try:
        _check_date(value, list(range(6)))
    except GS1ParseError as exc:
        error = exc.to_dict()
        error["message"] = f"rule {rule_index}: {field}: {error['message']}"
        raise RuleValidationError(
            {"rule_index": rule_index, "error": error}
        ) from exc
    return _expiry_date(value)


def _check_rules(rules: list[tuple]) -> list[dict]:
    """Validate every rule's expiry interval before any scan is parsed."""
    normalized = []
    for rule_index, (gtin, batches, expiry_from, expiry_to) in enumerate(rules):
        from_date = (
            _bound_date(rule_index, "expiry_from", expiry_from)
            if expiry_from is not None
            else None
        )
        to_date = (
            _bound_date(rule_index, "expiry_to", expiry_to)
            if expiry_to is not None
            else None
        )
        if from_date is not None and to_date is not None and from_date > to_date:
            raise RuleValidationError(
                {
                    "rule_index": rule_index,
                    "error": {
                        "error": "EXPIRY_RANGE_INVERTED",
                        "message": (
                            f"rule {rule_index}: expiry_from '{expiry_from}' "
                            f"({from_date}) is after expiry_to '{expiry_to}' "
                            f"({to_date})"
                        ),
                        "expected": "expiry_from on or before expiry_to",
                        "found": f"{expiry_from} > {expiry_to}",
                    },
                }
            )
        normalized.append(
            {
                "gtin": gtin,
                "batches": None if batches is None else list(batches),
                "expiry_from": expiry_from,
                "expiry_to": expiry_to,
                "from_date": from_date,
                "to_date": to_date,
            }
        )
    return normalized


def _batch_text(batch: str | None) -> str:
    return f"batch {batch!r}" if batch is not None else "no batch (AI 10 absent)"


def _conflict(index: int, first: dict, code: str, first_text: str, current_text: str):
    first_index = first["index"]
    raise ItemConflictError(
        {
            "scan_index": index,
            "first_index": first_index,
            "error": {
                "error": code,
                "message": (
                    f"scan {index}: item GTIN '{first['gtin']}' serial "
                    f"'{first['serial']}' was first scanned at index "
                    f"{first_index} with {first_text}, but this scan has "
                    f"{current_text}"
                ),
                "expected": f"{first_text} (scan {first_index})",
                "found": current_text,
            },
        }
    )


def _check_consistent(
    index: int,
    first: dict,
    batch: str | None,
    expiry: str,
    weight: Decimal,
    unit: str,
) -> None:
    """Reject a re-scan of ``first``'s item that disagrees on an attribute."""
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
            f"expiry '{first['expiry']}'",
            f"expiry '{expiry}'",
        )
    if unit != first["unit"] or weight != first["weight"]:
        _conflict(
            index,
            first,
            "CONFLICTING_WEIGHT",
            f"weight {first['weight']} {first['unit']}",
            f"weight {weight} {unit}",
        )


def _matches(rule: dict, item: dict) -> bool:
    """True when ``item`` falls inside the rule's GTIN/batch/expiry scope."""
    if item["gtin"] != rule["gtin"]:
        return False
    batches = rule["batches"]
    if batches is not None and item["batch"] not in batches:
        return False
    if rule["from_date"] is not None and item["expiry_date"] < rule["from_date"]:
        return False
    if rule["to_date"] is not None and item["expiry_date"] > rule["to_date"]:
        return False
    return True


def assess(scans: list[str], target_unit: str, rules: list[tuple]) -> dict:
    """Deduplicate scans into physical items and bucket them per rule.

    ``rules`` is a list of ``(gtin, batches, expiry_from, expiry_to)``
    4-tuples where ``batches`` is ``None`` (any batch) or a list of
    acceptable AI 10 values (a ``None`` entry matching batch-less items)
    and the expiry bounds are ``None`` or 6-digit YYMMDD strings forming
    an inclusive interval on the item's actual expiry date.  Rules are
    validated before the first scan is parsed.

    Returns ``{"target_unit": ..., "rules": [...], "unmatched": {...}}``
    with one rule entry per requested rule in the original order, each
    holding the echoed criteria, ``package_count`` and the unrounded
    :class:`Decimal` ``total_weight`` in the target unit; ``unmatched``
    aggregates the items no rule claimed.
    """
    normalized = _check_rules(rules)
    items: dict[tuple[str, str], dict] = {}

    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION
        for index, scan in enumerate(scans):
            try:
                elements = parse(scan)
            except GS1ParseError as exc:
                raise _failure(index, exc) from exc
            try:
                gtin, batch, expiry, serial, weight, unit = _extract(
                    index, elements
                )
            except ScanValidationError as exc:
                raise _failure(index, exc) from exc

            key = (gtin, serial)
            first = items.get(key)
            if first is not None:
                _check_consistent(index, first, batch, expiry, weight, unit)
                continue  # identical re-scan: the item is counted once
            items[key] = {
                "index": index,
                "gtin": gtin,
                "serial": serial,
                "batch": batch,
                "expiry": expiry,
                "expiry_date": _expiry_date(expiry),
                "weight": weight,
                "unit": unit,
                "converted": _to_target(weight, unit, target_unit),
                "matched": False,
            }

        rule_results = []
        for rule_index, rule in enumerate(normalized):
            count = 0
            total = Decimal(0)
            for item in items.values():
                if item["matched"] or not _matches(rule, item):
                    continue
                item["matched"] = True
                count += 1
                total += item["converted"]
            rule_results.append(
                {
                    "rule_index": rule_index,
                    "gtin": rule["gtin"],
                    "batches": rule["batches"],
                    "expiry_from": rule["expiry_from"],
                    "expiry_to": rule["expiry_to"],
                    "package_count": count,
                    "unit": target_unit,
                    "total_weight": total,
                }
            )

        unmatched_count = 0
        unmatched_total = Decimal(0)
        for item in items.values():
            if not item["matched"]:
                unmatched_count += 1
                unmatched_total += item["converted"]

    return {
        "target_unit": target_unit,
        "rules": rule_results,
        "unmatched": {
            "package_count": unmatched_count,
            "unit": target_unit,
            "total_weight": unmatched_total,
        },
    }
