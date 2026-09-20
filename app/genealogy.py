"""Production batch genealogy audit over parsed GS1 scans.

The request carries an opening batch inventory plus a set of
transformation processes in arbitrary order.  Every scan -- an opening
stock line, a process input or a process output -- is fed through
:func:`app.gs1.parse` and must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 10 (batch/lot),
* exactly one weight element of the 310n (kg) / 320n (lb) family.

AI 17/21 are ignored when present.  Any parser failure or
missing/duplicated required AI fails the whole request with
:class:`GenealogyError` whose ``detail`` names the offending scan:
``stock_index`` for opening stock, ``process_index`` plus
``input_index`` / ``output_index`` for process scans -- the same 422
shape as ``/weight-summary`` & friends.

Batches are identified by (GTIN, batch).  Every weight is rebuilt with
:class:`decimal.Decimal` from the original six digits and converted to
the target unit with the fixed factors of :mod:`app.summary`; repeated
scans of one batch are aggregated by exact Decimal addition.

Before any stock moves, the production/consumption dependency graph is
built: a process depends on the process that produces a batch it
consumes.  The whole request fails with :class:`ProcessGraphError`
(whose ``detail`` names the offending ``process_index``) when

* one batch is produced by more than one process
  (``MULTIPLE_PRODUCERS``; the detail also carries ``first_index``, the
  first process that produced the batch),
* a process consumes a batch that is neither in the opening stock nor
  produced by any process (``UNKNOWN_BATCH``),
* the dependencies form a cycle (``PROCESS_CYCLE``; the detail names
  the smallest index among the processes locked in the cycle).

The processes then run in a stable topological order -- dependencies
first, ties broken by the original process index -- and every input is
deducted from the running stock; a process that needs more of a batch
than is available fails the whole request with
:class:`InsufficientStockError` naming the ``process_index``.

The result reports the processes in that stable topological order, each
with its original ``process_index``, its per-batch inputs and outputs
and the input/output/loss weight totals (loss = input - output), plus
the final remaining quantity of every known batch: opening-stock
batches in first-seen order, then produced batches in execution order.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from heapq import heapify, heappop, heappush

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import (
    ARITHMETIC_PRECISION,
    ScanValidationError,
    _to_target,
)

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 10 (batch/lot), "
    "and exactly one weight AI 310n/320n"
)

SIX_PLACES = Decimal("0.000001")


class GenealogyError(Exception):
    """A scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: the scan location
    (``stock_index``, or ``process_index`` with ``input_index`` /
    ``output_index``) plus the original parser/validation error under
    ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class ProcessGraphError(Exception):
    """The production/consumption dependency graph is invalid.

    ``detail`` is the complete 422 payload: the offending
    ``process_index`` (plus ``first_index`` for a repeated producer)
    and the graph error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class InsufficientStockError(Exception):
    """A process consumes more of a batch than the stock holds.

    ``detail`` is the complete 422 payload naming the ``process_index``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _extract(
    label: str, elements: list[Element]
) -> tuple[str, str, Decimal, str]:
    """Pull (GTIN, batch, exact weight, weight unit) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
    batches = [e for e in elements if e.ai == "10"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"{label}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{label}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(batches) != 1:
        if not batches:
            raise ScanValidationError(
                "MISSING_AI",
                f"{label}: missing required AI 10 (batch/lot)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{label}: AI 10 (batch/lot) appears {len(batches)} times; exactly one is required",
            expected="exactly one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"{label}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{label}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return gtins[0].value, batches[0].value, value, weight.unit


def _load_scan(scan: str, label: str, location: dict):
    """Parse one scan and extract its batch weight.

    ``location`` is the 422 detail prefix identifying the scan
    (``{"stock_index": i}`` or ``{"process_index": i, "input_index": j}``
    / ``{"output_index": j}``); ``label`` is its human-readable form for
    error messages.
    """
    try:
        elements = parse(scan)
    except GS1ParseError as exc:
        raise GenealogyError({**location, "error": exc.to_dict()}) from exc
    try:
        return _extract(label, elements)
    except ScanValidationError as exc:
        raise GenealogyError({**location, "error": exc.to_dict()}) from exc


def _aggregate(scans: list[str], target_unit: str, describe):
    """Aggregate scans into ``{(gtin, batch): weight}`` in first-seen order.

    ``describe(index)`` returns ``(label, location)`` for one scan: the
    human label used in error messages and the 422 detail prefix
    identifying the scan.
    """
    weights: dict[tuple[str, str], Decimal] = {}
    order: list[tuple[str, str]] = []
    for index, scan in enumerate(scans):
        label, location = describe(index)
        gtin, batch, weight, unit = _load_scan(scan, label, location)
        key = (gtin, batch)
        converted = _to_target(weight, unit, target_unit)
        if key not in weights:
            order.append(key)
            weights[key] = Decimal(0)
        weights[key] += converted
    return weights, order


def _batch_text(key: tuple[str, str]) -> str:
    return f"GTIN '{key[0]}' batch '{key[1]}'"


def _six_text(value: Decimal) -> str:
    """Six-place half-even text of ``value`` for error messages."""
    return format(value.quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN), "f")


def _check_single_producer(procs: list[dict]) -> dict[tuple[str, str], int]:
    """Map every produced batch to its process; reject second producers."""
    producer: dict[tuple[str, str], int] = {}
    for proc in procs:
        for key in proc["output_order"]:
            first = producer.get(key)
            if first is not None:
                index = proc["index"]
                raise ProcessGraphError(
                    {
                        "process_index": index,
                        "first_index": first,
                        "error": {
                            "error": "MULTIPLE_PRODUCERS",
                            "message": (
                                f"process {index}: batch {_batch_text(key)} is "
                                f"already produced by process {first}; each "
                                "batch may be produced by at most one process"
                            ),
                            "expected": (
                                "each batch (GTIN + AI 10) to be produced by "
                                "at most one process"
                            ),
                            "found": f"produced by processes {first} and {index}",
                        },
                    }
                )
            producer[key] = proc["index"]
    return producer


def _dependency_edges(procs: list[dict], stock: dict, producer: dict):
    """Build (dependents, indegree); reject inputs without a source.

    A process depends on the process producing a batch it consumes
    (a batch only in the opening stock adds no edge); a process
    consuming its own output depends on itself and is a cycle.
    """
    dependents: dict[int, list[int]] = {proc["index"]: [] for proc in procs}
    indegree: dict[int, int] = {proc["index"]: 0 for proc in procs}
    for proc in procs:
        index = proc["index"]
        seen_sources = set()
        for key in proc["input_order"]:
            if key not in stock and key not in producer:
                raise ProcessGraphError(
                    {
                        "process_index": index,
                        "error": {
                            "error": "UNKNOWN_BATCH",
                            "message": (
                                f"process {index}: input batch "
                                f"{_batch_text(key)} has no source: it is "
                                "neither in the opening stock nor produced "
                                "by any process"
                            ),
                            "expected": (
                                "every input batch to appear in the opening "
                                "stock or in some process's outputs"
                            ),
                            "found": f"batch {_batch_text(key)} without a source",
                        },
                    }
                )
            source = producer.get(key)
            if source is None or source in seen_sources:
                continue
            seen_sources.add(source)
            dependents[source].append(index)
            indegree[index] += 1
    return dependents, indegree


def _topological_order(procs: list[dict], dependents: dict, indegree: dict):
    """Kahn's algorithm; among ready processes the smallest index goes first."""
    ready = [index for index, degree in indegree.items() if degree == 0]
    heapify(ready)
    order = []
    while ready:
        index = heappop(ready)
        order.append(index)
        for dependent in dependents[index]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heappush(ready, dependent)

    if len(order) < len(procs):
        remaining = sorted(set(indegree) - set(order))
        cycle_text = ", ".join(str(i) for i in remaining)
        raise ProcessGraphError(
            {
                "process_index": remaining[0],
                "error": {
                    "error": "PROCESS_CYCLE",
                    "message": (
                        f"process {remaining[0]}: production/consumption "
                        "dependencies form a cycle among process(es) "
                        f"{cycle_text}"
                    ),
                    "expected": "an acyclic production/consumption dependency graph",
                    "found": f"cyclic dependencies among process(es) {cycle_text}",
                },
            }
        )
    return order


def genealogy(
    opening_stock: list[str],
    processes: list[tuple[list[str], list[str]]],
    target_unit: str,
) -> dict:
    """Audit a production batch genealogy against the opening stock.

    ``opening_stock`` is a list of scans and ``processes`` a list of
    ``(inputs, outputs)`` pairs of scan lists, in arbitrary order;
    every scan carries exactly one AI 01, one AI 10 and one weight AI.
    Batches are identified by (GTIN, batch) and every weight is
    converted to ``target_unit`` with exact :class:`Decimal` arithmetic.

    Returns ``{"target_unit": ..., "processes": [...], "batches":
    [...]}`` where ``processes`` holds one entry per process in stable
    topological order -- ``process_index`` (the original position), the
    per-batch ``inputs`` / ``outputs`` in first-seen order and the
    ``input_weight`` / ``output_weight`` / ``loss_weight`` totals -- and
    ``batches`` holds the final ``remaining`` weight of every known
    batch (opening stock first, then produced batches in execution
    order).  All numeric values are unrounded :class:`Decimal`.
    """
    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION

        stock, stock_order = _aggregate(
            opening_stock,
            target_unit,
            lambda index: (
                f"opening stock scan {index}",
                {"stock_index": index},
            ),
        )

        procs = []
        for process_index, (input_scans, output_scans) in enumerate(processes):
            inputs, input_order = _aggregate(
                input_scans,
                target_unit,
                lambda index, i=process_index: (
                    f"process {i} input {index}",
                    {"process_index": i, "input_index": index},
                ),
            )
            outputs, output_order = _aggregate(
                output_scans,
                target_unit,
                lambda index, i=process_index: (
                    f"process {i} output {index}",
                    {"process_index": i, "output_index": index},
                ),
            )
            procs.append(
                {
                    "index": process_index,
                    "inputs": inputs,
                    "input_order": input_order,
                    "outputs": outputs,
                    "output_order": output_order,
                }
            )

        # ---- dependency graph: producers, sources, cycles ----
        producer = _check_single_producer(procs)
        dependents, indegree = _dependency_edges(procs, stock, producer)
        order = _topological_order(procs, dependents, indegree)

        # ---- execute: deduct inputs, then add outputs ----
        inventory = dict(stock)
        batch_order = list(stock_order)
        for index in order:
            proc = procs[index]
            for key in proc["input_order"]:
                need = proc["inputs"][key]
                have = inventory.get(key, Decimal(0))
                if need > have:
                    raise InsufficientStockError(
                        {
                            "process_index": index,
                            "error": {
                                "error": "INSUFFICIENT_STOCK",
                                "message": (
                                    f"process {index}: input batch "
                                    f"{_batch_text(key)} requires "
                                    f"{_six_text(need)} {target_unit} but only "
                                    f"{_six_text(have)} {target_unit} is "
                                    "available"
                                ),
                                "expected": (
                                    f"at most {_six_text(have)} {target_unit} "
                                    f"of batch {_batch_text(key)}"
                                ),
                                "found": f"requested {_six_text(need)} {target_unit}",
                            },
                        }
                    )
                inventory[key] = have - need
            for key in proc["output_order"]:
                if key not in inventory:
                    inventory[key] = Decimal(0)
                    batch_order.append(key)
                inventory[key] += proc["outputs"][key]

        process_results = []
        for index in order:
            proc = procs[index]
            input_total = sum(proc["inputs"].values(), Decimal(0))
            output_total = sum(proc["outputs"].values(), Decimal(0))
            process_results.append(
                {
                    "process_index": index,
                    "inputs": [
                        {
                            "gtin": key[0],
                            "batch": key[1],
                            "weight": proc["inputs"][key],
                        }
                        for key in proc["input_order"]
                    ],
                    "outputs": [
                        {
                            "gtin": key[0],
                            "batch": key[1],
                            "weight": proc["outputs"][key],
                        }
                        for key in proc["output_order"]
                    ],
                    "input_weight": input_total,
                    "output_weight": output_total,
                    "loss_weight": input_total - output_total,
                }
            )

        return {
            "target_unit": target_unit,
            "processes": process_results,
            "batches": [
                {
                    "gtin": key[0],
                    "batch": key[1],
                    "remaining": inventory[key],
                }
                for key in batch_order
            ],
        }
