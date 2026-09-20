"""Weight aggregation for batches of parsed GS1 scans.

Each scan is fed through :func:`app.gs1.parse`; it must then contain:

* exactly one AI 01 (GTIN),
* zero or one AI 10 (batch/lot) -- optional,
* exactly one weight element of the 310n (kg) / 320n (lb) family.

Any parser failure, missing/duplicated required AI or multiple weight
elements make the whole request fail with :class:`WeightSummaryError`
whose ``detail`` names the offending scan index.  Valid scans are
grouped by (GTIN, batch) in first-seen order and their weights are
rebuilt with :class:`decimal.Decimal` straight from the original six
digits and the precision digit -- the float ``decimal_value`` produced
by the parser is never used for arithmetic.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse

# Exact international pound: 1 lb = 0.45359237 kg
LB_TO_KG = Decimal("0.45359237")

# Plenty of headroom for multiplying/dividing the 10-digit integer weights
# by the conversion factor before the final six-place rounding.
ARITHMETIC_PRECISION = 50

with localcontext() as ctx:
    ctx.prec = ARITHMETIC_PRECISION
    KG_TO_LB = Decimal(1) / LB_TO_KG  # reciprocal: 1 kg in lb, 50 significant digits

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), at most one AI 10 (batch/lot), "
    "and exactly one weight AI 310n/320n"
)


class WeightSummaryError(Exception):
    """A scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: ``scan_index`` plus the
    original parser/validation error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class ScanValidationError(GS1ParseError):
    """Structural (post-parse) failure within a single scan.

    Carries the same error/message/expected/found shape as parser errors
    so it can surface in a 422 detail untouched.
    """

    def __init__(self, code, message, expected, found=None, offset=None):
        super().__init__(code, message, offset=offset, expected=expected, found=found)


def _failure(index: int, error: GS1ParseError) -> WeightSummaryError:
    """Wrap a parser/validation error with the offending scan index."""
    return WeightSummaryError({"scan_index": index, "error": error.to_dict()})


def _extract(
    index: int, elements: list[Element]
) -> tuple[str, str | None, Decimal, str]:
    """Pull (GTIN, batch or None, exact weight, weight unit) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
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
    return gtins[0].value, batches[0].value if batches else None, value, weight.unit


def _to_target(weight: Decimal, unit: str, target_unit: str) -> Decimal:
    """Convert an exact weight into the target unit."""
    if unit == target_unit:
        return weight
    if unit == "lb" and target_unit == "kg":
        return weight * LB_TO_KG
    return weight * KG_TO_LB  # kg -> lb


def summarize(scans: list[str], target_unit: str) -> dict:
    """Parse every scan, validate its shape and aggregate weights per group.

    Returns ``{"target_unit": ..., "groups": [{gtin, batch, count, unit,
    total}, ...]}`` with groups ordered by first appearance and ``total``
    an unrounded :class:`Decimal` already expressed in the target unit.
    """
    groups: dict[tuple[str, str | None], dict] = {}
    order: list[tuple[str, str | None]] = []

    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION
        for index, scan in enumerate(scans):
            try:
                elements = parse(scan)
            except GS1ParseError as exc:
                raise _failure(index, exc) from exc
            try:
                gtin, batch, weight, unit = _extract(index, elements)
            except ScanValidationError as exc:
                raise _failure(index, exc) from exc

            key = (gtin, batch)
            converted = _to_target(weight, unit, target_unit)
            if key not in groups:
                order.append(key)
                groups[key] = {
                    "gtin": gtin,
                    "batch": batch,
                    "count": 0,
                    "unit": target_unit,
                    "total": Decimal(0),
                }
            groups[key]["count"] += 1
            groups[key]["total"] += converted

    return {"target_unit": target_unit, "groups": [groups[key] for key in order]}
