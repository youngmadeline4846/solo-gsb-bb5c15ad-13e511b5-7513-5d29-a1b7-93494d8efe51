"""FEFO pick-planning of net weights across parsed GS1 scans.

Each scan is fed through :func:`app.gs1.parse` and must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 17 (expiry date; day ``00`` denotes the last day of the
  month, per the GS1 General Specifications),
* zero or one AI 10 (batch/lot) -- optional,
* exactly one weight element of the 310n (kg) / 320n (lb) family.

Any parser failure, missing/duplicated required AI or multiple weight
elements make the whole request fail with
:class:`app.summary.WeightSummaryError` whose ``detail`` names the
offending scan index -- the same 422 shape as ``/weight-summary``.

Valid scans are aggregated by (GTIN, batch, AI 17 value) in first-seen
order; every weight is rebuilt with :class:`decimal.Decimal` from the
original six digits and converted with the fixed factors of
:mod:`app.summary`.  Each demand is then satisfied FEFO: the GTIN's
groups are walked by ascending *actual* expiry date (day ``00`` -> last
day of the month), ties keep the batches' first-appearance order (the
sort is stable), and the last group reached may be allocated partially.

Every demand may carry a non-negative ``min_remaining_days`` (default
0): given the request's ``as_of`` date, only groups whose actual expiry
date is on or after ``as_of + min_remaining_days`` may serve that
demand -- the boundary date itself is eligible.  Excluded groups never
contribute to ``fulfilled`` (the shortfall stays in ``shortage``); their
combined weight per GTIN is reported as ``excluded_weight``.  A negative
``min_remaining_days`` fails the whole request with
:class:`DemandValidationError` naming the offending ``demand_index``.

Each demand may also choose a ``package_mode``: ``"splittable"`` (the
default) behaves as described above, while ``"whole"`` treats every
eligible scan as an indivisible package.  Whole-package demands are
planned on the individual scans (never aggregated): the demand GTIN's
packages that pass the shelf-life threshold are sorted FEFO (ascending
actual expiry date, ties keep the original scan order) and, when some
subset reaches ``quantity``, the subset with the smallest overage is
chosen; ties are broken by the lexicographically smallest index
sequence in that FEFO order.  When no subset reaches ``quantity``,
every eligible package is taken.  A whole-package demand with more than
:data:`MAX_WHOLE_PACKAGES` eligible packages fails the whole request
with :class:`DemandValidationError` naming the ``demand_index``.
Whole-mode ``allocations`` hold one entry per selected scan in FEFO
order, ``overage`` reports any overshoot beyond ``quantity`` and
``shortage`` never goes below zero.

All conversion, aggregation, comparison and allocation arithmetic stays
in :class:`Decimal`; rounding to six places happens only when the
response is rendered.
"""

from __future__ import annotations

import calendar
from bisect import bisect_left
from datetime import date, timedelta
from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import (
    ARITHMETIC_PRECISION,
    ScanValidationError,
    WeightSummaryError,
    _failure,
    _to_target,
)

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 17 (expiry date), "
    "at most one AI 10 (batch/lot), and exactly one weight AI 310n/320n"
)

PACKAGE_MODES = ("splittable", "whole")

# Whole-package planning enumerates subsets meet-in-the-middle, which
# stays exact and fast only up to this many eligible packages per demand.
MAX_WHOLE_PACKAGES = 30


class DemandValidationError(Exception):
    """A demand violated the request shape (e.g. a duplicated GTIN).

    ``detail`` is the complete 422 payload: ``demand_index`` plus the
    validation error under ``error``, mirroring the scan-level shape.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _expiry_date(yymmdd: str) -> date:
    """Actual calendar date of an AI 17 value; day ``00`` = last day of month."""
    year = 2000 + int(yymmdd[0:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    if day == 0:
        day = calendar.monthrange(year, month)[1]
    return date(year, month, day)


def _threshold(as_of: date, min_remaining_days: int) -> date:
    """Earliest eligible actual expiry date for one demand."""
    try:
        return as_of + timedelta(days=min_remaining_days)
    except OverflowError:
        # A threshold beyond any representable date excludes everything.
        return date.max


def _extract(
    index: int, elements: list[Element]
) -> tuple[str, str | None, str, Decimal, str]:
    """Pull (GTIN, batch or None, AI 17 value, exact weight, unit) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
    expiries = [e for e in elements if e.ai == "17"]
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
        value,
        weight.unit,
    )


def _check_demands(demands: list[tuple[str, Decimal, int, str]]) -> None:
    """Reject bad thresholds/modes and GTINs requested by more than one demand."""
    seen: dict[str, int] = {}
    for demand_index, (
        gtin,
        _quantity,
        min_remaining_days,
        package_mode,
    ) in enumerate(demands):
        if min_remaining_days < 0:
            raise DemandValidationError(
                {
                    "demand_index": demand_index,
                    "error": {
                        "error": "INVALID_MIN_REMAINING_DAYS",
                        "message": (
                            f"demand {demand_index}: min_remaining_days must be "
                            f"a non-negative integer, got {min_remaining_days}"
                        ),
                        "expected": "non-negative integer min_remaining_days",
                        "found": str(min_remaining_days),
                    },
                }
            )
        if package_mode not in PACKAGE_MODES:
            raise DemandValidationError(
                {
                    "demand_index": demand_index,
                    "error": {
                        "error": "INVALID_PACKAGE_MODE",
                        "message": (
                            f"demand {demand_index}: package_mode must be "
                            f"'splittable' or 'whole', got {package_mode!r}"
                        ),
                        "expected": "package_mode 'splittable' or 'whole'",
                        "found": str(package_mode),
                    },
                }
            )
        if gtin in seen:
            first = seen[gtin]
            raise DemandValidationError(
                {
                    "demand_index": demand_index,
                    "error": {
                        "error": "DUPLICATE_GTIN",
                        "message": (
                            f"demand {demand_index}: GTIN '{gtin}' is already "
                            f"requested by demand {first}; each GTIN may "
                            "appear only once"
                        ),
                        "expected": "unique GTIN across all demands",
                        "found": gtin,
                    },
                }
            )
        seen[gtin] = demand_index


def _suffix_sum_sets(weights: list[Decimal]) -> list[set[Decimal]]:
    """Suffix subset sums: entry ``i`` holds every sum a subset of
    ``weights[i:]`` can reach (the empty subset contributes 0).

    Only ever built for halves of at most ``MAX_WHOLE_PACKAGES // 2``
    packages, so each set holds at most 2**15 sums.
    """
    sets: list[set[Decimal]] = [set() for _ in range(len(weights) + 1)]
    sets[len(weights)] = {Decimal(0)}
    for i in range(len(weights) - 1, -1, -1):
        previous = sets[i + 1]
        sets[i] = previous | {weights[i] + s for s in previous}
    return sets


def _reachable(
    a_sets: list[set[Decimal]],
    b_sets: list[set[Decimal]],
    split: int,
    start: int,
    target: Decimal,
) -> bool:
    """True when some subset of ``packages[start:]`` sums exactly to ``target``.

    ``a_sets``/``b_sets`` are the suffix subset-sum sets of the package
    halves ``[0, split)`` and ``[split, n)``.
    """
    if start >= split:
        return target in b_sets[start - split]
    b_all = b_sets[0]
    return any(target - s in b_all for s in a_sets[start])


def _select_packages(weights: list[Decimal], quantity: Decimal) -> list[int] | None:
    """Indices of the whole-package subset serving ``quantity``.

    Returns the subset whose total is the smallest to reach ``quantity``
    (smallest overage); ties are broken by the lexicographically
    smallest index sequence.  Returns ``None`` when no subset reaches
    ``quantity``.  Meet-in-the-middle keeps this exact for up to
    ``MAX_WHOLE_PACKAGES`` packages.
    """
    n = len(weights)
    split = n // 2
    a_sets = _suffix_sum_sets(weights[:split])
    b_sets = _suffix_sum_sets(weights[split:])

    # Smallest achievable total >= quantity: for every sum of the first
    # half, take the smallest sufficient sum of the second half.
    b_all_sorted = sorted(b_sets[0])
    best: Decimal | None = None
    for a_sum in a_sets[0]:
        i = bisect_left(b_all_sorted, quantity - a_sum)
        if i < len(b_all_sorted):
            total = a_sum + b_all_sorted[i]
            if best is None or total < best:
                best = total
    if best is None:
        return None

    # Rebuild the lexicographically smallest index sequence totalling
    # ``best``: the next index is the smallest j after which a completing
    # subset still exists, which the suffix subset sums answer directly.
    chosen = []
    remaining = best
    start = 0
    while remaining > 0:
        for j in range(start, n):
            if _reachable(a_sets, b_sets, split, j + 1, remaining - weights[j]):
                chosen.append(j)
                remaining -= weights[j]
                start = j + 1
                break
        else:  # unreachable by construction: ``best`` is achievable
            raise AssertionError("no package can complete the selected subset")
    return chosen


def _whole_plan(
    demand_index: int,
    gtin: str,
    quantity: Decimal,
    threshold: date,
    packages: list[dict],
) -> dict:
    """Plan one whole-package demand: every eligible scan is indivisible.

    ``packages`` are the demand GTIN's scans in original scan order.
    """
    eligible = []
    excluded = Decimal(0)
    for package in packages:
        if package["expiry_date"] < threshold:
            excluded += package["weight"]
        else:
            eligible.append(package)

    if len(eligible) > MAX_WHOLE_PACKAGES:
        raise DemandValidationError(
            {
                "demand_index": demand_index,
                "error": {
                    "error": "TOO_MANY_PACKAGES",
                    "message": (
                        f"demand {demand_index}: {len(eligible)} eligible "
                        f"packages for GTIN '{gtin}' exceed the whole-package "
                        f"limit of {MAX_WHOLE_PACKAGES}"
                    ),
                    "expected": (
                        f"at most {MAX_WHOLE_PACKAGES} eligible packages "
                        "per whole-package demand"
                    ),
                    "found": str(len(eligible)),
                },
            }
        )

    # FEFO: ascending actual expiry date.  The sort is stable, so
    # packages expiring on the same date keep their original scan order.
    eligible.sort(key=lambda package: package["expiry_date"])
    weights = [package["weight"] for package in eligible]
    chosen = _select_packages(weights, quantity)
    if chosen is None:
        # No subset reaches quantity: take every eligible package.
        chosen = list(range(len(eligible)))
    fulfilled = sum((weights[j] for j in chosen), Decimal(0))
    return {
        "gtin": gtin,
        "quantity": quantity,
        "fulfilled": fulfilled,
        "shortage": max(quantity - fulfilled, Decimal(0)),
        "overage": max(fulfilled - quantity, Decimal(0)),
        "excluded_weight": excluded,
        "allocations": [
            {
                "batch": eligible[j]["batch"],
                "expiry": eligible[j]["expiry"],
                "weight": weights[j],
            }
            for j in chosen
        ],
    }


def pick_plan(
    scans: list[str],
    target_unit: str,
    demands: list[tuple],
    as_of: date,
) -> dict:
    """Aggregate scan weights per GTIN and satisfy each demand.

    ``demands`` is a list of ``(gtin, quantity, min_remaining_days)``
    triples or ``(gtin, quantity, min_remaining_days, package_mode)``
    4-tuples (``package_mode`` defaults to ``"splittable"``) with
    ``quantity`` a :class:`Decimal` already expressed in ``target_unit``
    and ``min_remaining_days`` a non-negative integer; a GTIN may appear
    only once.  Only stock whose actual expiry date is on or after
    ``as_of + min_remaining_days`` may serve a demand.  Splittable
    demands are satisfied FEFO from the aggregated groups with a
    possibly partial final allocation; whole-package demands treat every
    eligible scan as indivisible and are planned per scan (see the
    module docstring).

    Returns ``{"target_unit": ..., "demands": [{gtin, quantity,
    fulfilled, shortage, overage, excluded_weight, allocations}, ...]}``
    in demand order, where every numeric value is an unrounded
    :class:`Decimal` in the target unit, ``excluded_weight`` is the
    total weight of the demand's GTIN held in stock the threshold ruled
    out, ``overage`` is the overshoot beyond ``quantity`` (zero for
    splittable demands), ``shortage`` is the unmet remainder (never
    negative), and each allocation is ``{"batch", "expiry", "weight"}``
    with ``expiry`` the raw AI 17 value of the source group or scan.
    """
    normalized = []
    for demand in demands:
        if len(demand) == 3:
            demand = (*demand, "splittable")
        normalized.append(tuple(demand))
    _check_demands(normalized)

    groups: dict[tuple[str, str | None, str], dict] = {}
    order: list[tuple[str, str | None, str]] = []
    packages_by_gtin: dict[str, list[dict]] = {}

    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION
        for index, scan in enumerate(scans):
            try:
                elements = parse(scan)
            except GS1ParseError as exc:
                raise _failure(index, exc) from exc
            try:
                gtin, batch, expiry, weight, unit = _extract(index, elements)
            except ScanValidationError as exc:
                raise _failure(index, exc) from exc

            key = (gtin, batch, expiry)
            converted = _to_target(weight, unit, target_unit)
            if key not in groups:
                order.append(key)
                groups[key] = {
                    "gtin": gtin,
                    "batch": batch,
                    "expiry": expiry,
                    "expiry_date": _expiry_date(expiry),
                    "available": Decimal(0),
                }
            groups[key]["available"] += converted
            packages_by_gtin.setdefault(gtin, []).append(
                {
                    "batch": batch,
                    "expiry": expiry,
                    "expiry_date": groups[key]["expiry_date"],
                    "weight": converted,
                }
            )

        by_gtin: dict[str, list[dict]] = {}
        for key in order:
            by_gtin.setdefault(key[0], []).append(groups[key])
        for gtin_groups in by_gtin.values():
            # FEFO: ascending actual expiry date.  The sort is stable, so
            # groups expiring on the same date keep their first-seen order.
            gtin_groups.sort(key=lambda group: group["expiry_date"])

        results = []
        for demand_index, (
            gtin,
            quantity,
            min_remaining_days,
            package_mode,
        ) in enumerate(normalized):
            threshold = _threshold(as_of, min_remaining_days)
            if package_mode == "whole":
                results.append(
                    _whole_plan(
                        demand_index,
                        gtin,
                        quantity,
                        threshold,
                        packages_by_gtin.get(gtin, []),
                    )
                )
                continue
            remaining = quantity
            allocations = []
            excluded = Decimal(0)
            # Groups are sorted by ascending actual expiry, so every
            # group the threshold rules out (expiry before as_of +
            # min_remaining_days) forms a prefix of the walk; their
            # weight is summed into excluded_weight and they never
            # contribute to fulfilled.
            for group in by_gtin.get(gtin, []):
                if group["expiry_date"] < threshold:
                    excluded += group["available"]
                    continue
                if remaining <= 0:
                    break
                take = min(remaining, group["available"])
                if take <= 0:
                    continue  # group of zero-weight scans contributes nothing
                allocations.append(
                    {
                        "batch": group["batch"],
                        "expiry": group["expiry"],
                        "weight": take,
                    }
                )
                remaining -= take
            results.append(
                {
                    "gtin": gtin,
                    "quantity": quantity,
                    "fulfilled": quantity - remaining,
                    "shortage": remaining,
                    "overage": Decimal(0),
                    "excluded_weight": excluded,
                    "allocations": allocations,
                }
            )

    return {"target_unit": target_unit, "demands": results}
