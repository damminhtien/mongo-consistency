"""Offline RQ4 consistency, completion, and critical-operation latency analysis."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .checkers import check_history
from .history import read_history
from .models import History, Outcome

ROOT = Path(__file__).resolve().parents[2]
OUTCOMES = ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
CONFIGURATION_IDS = tuple(f"C{number}" for number in range(1, 9))
PROPERTIES = ("RYW", "MR", "MW", "WFR")
CRITICAL_STEPS = {
    "RYW": "read",
    "MR": "second_read",
    "MW": "second_write",
    "WFR": "write",
}
SCENARIO_ORDER = (
    "normal",
    "rq1_fault",
    "secondary_crash",
    "primary_crash",
    "partition",
)
SCENARIO_LABELS = {"F1": "secondary_crash", "F2": "primary_crash", "F3": "partition"}
CONTRASTS = (
    ("C1", "C2", "causal_session"),
    ("C7", "C3", "causal_session"),
    ("C8", "C4", "causal_session"),
    ("C5", "C6", "causal_session"),
)
PARTITION_SIGNATURES = (
    ("C1", "RYW"),
    ("C6", "RYW"),
    ("C1", "MW"),
    ("C6", "MW"),
)


def quantile(values: Iterable[float], probability: float) -> float | None:
    """Return a deterministic linear-interpolated quantile."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _interval_ms(start_ns: Any, end_ns: Any) -> float | None:
    if (
        isinstance(start_ns, int)
        and not isinstance(start_ns, bool)
        and isinstance(end_ns, int)
        and not isinstance(end_ns, bool)
        and end_ns >= start_ns
    ):
        return (end_ns - start_ns) / 1_000_000
    return None


def _scenario(manifest: dict[str, Any]) -> str:
    """Map stored campaign fields to a stable RQ4 scenario label."""

    if manifest.get("campaign_id") == "experiment":
        return "normal" if not manifest.get("adversarial", False) else "rq1_fault"
    if manifest.get("campaign_id") == "rq2":
        return SCENARIO_LABELS.get(str(manifest.get("topology_condition")), "rq2_unknown")
    return str(manifest.get("campaign_id") or "unknown")


def _critical_operation(history: History) -> tuple[str, Any | None]:
    """Return the registered critical step and its operation record."""

    property_name = str(history.manifest.get("property") or "")
    step = CRITICAL_STEPS.get(property_name, "")
    property_steps = history.manifest.get("property_steps")
    if not isinstance(property_steps, dict):
        property_steps = {}
    operation_id = property_steps.get(step, step)
    for operation in history.operations:
        if operation.operation_id == operation_id:
            return str(operation_id), operation
    return str(operation_id), None


def _record(path: Path, raw_root: Path, campaign: str) -> dict[str, Any]:
    """Read one immutable history and expose only RQ4 analysis fields."""

    relative = path.relative_to(raw_root).as_posix()
    try:
        history = read_history(path)
    except Exception as error:  # noqa: BLE001 - malformed raw input remains visible.
        return {
            "path": relative,
            "campaign": campaign,
            "campaign_id": None,
            "configuration_id": None,
            "scenario": "unknown",
            "fault_condition": None,
            "property": None,
            "outcome": Outcome.HARNESS_ERROR.value,
            "critical_operation": None,
            "latency_ms": None,
            "error": f"history could not be loaded: {error}",
        }

    result = check_history(history)
    manifest = history.manifest
    operation_id, operation = _critical_operation(history)
    latency_ms = (
        _interval_ms(operation.start_ns, operation.end_ns) if operation is not None else None
    )
    return {
        "path": relative,
        "campaign": campaign,
        "campaign_id": manifest.get("campaign_id"),
        "configuration_id": manifest.get("configuration_id"),
        "scenario": _scenario(manifest),
        "fault_condition": manifest.get("topology_condition"),
        "property": manifest.get("property"),
        "outcome": result.outcome.value,
        "critical_operation": operation_id,
        "critical_operation_status": (
            operation.operation_status if operation is not None else None
        ),
        "response_received": operation.response_received if operation is not None else None,
        "latency_ms": latency_ms,
        "history_hash": history.history_hash,
    }


def _history_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.json")
        if path.name != "campaign-manifest.json"
        and ".rq2-staging" not in path.relative_to(root).parts
    )


def _campaign_roots(raw_root: Path) -> list[tuple[str, Path]]:
    """Resolve the brief's rq1/rq2 names and the repository's experiment name."""

    if raw_root.name in {"rq1", "experiment"}:
        return [("rq1", raw_root)]
    if raw_root.name == "rq2":
        return [("rq2", raw_root)]

    roots: list[tuple[str, Path]] = []
    rq1_root = raw_root / "rq1"
    experiment_root = raw_root / "experiment"
    if rq1_root.is_dir():
        roots.append(("rq1", rq1_root))
    elif experiment_root.is_dir():
        roots.append(("rq1", experiment_root))
    rq2_root = raw_root / "rq2"
    if rq2_root.is_dir():
        roots.append(("rq2", rq2_root))
    return roots


def load_records(raw_root: Path) -> list[dict[str, Any]]:
    """Load only RQ1 and RQ2 histories; no live database calls are made."""

    records: list[dict[str, Any]] = []
    for campaign, root in _campaign_roots(raw_root):
        records.extend(_record(path, raw_root, campaign) for path in _history_paths(root))
    return sorted(records, key=lambda record: str(record["path"]))


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _sort_key(key: tuple[str, str, str]) -> tuple[int, int, int]:
    configuration, scenario, property_name = key
    return (
        CONFIGURATION_IDS.index(configuration) if configuration in CONFIGURATION_IDS else 99,
        SCENARIO_ORDER.index(scenario) if scenario in SCENARIO_ORDER else 99,
        PROPERTIES.index(property_name) if property_name in PROPERTIES else 99,
    )


def _empty_counts() -> dict[str, int]:
    return {outcome: 0 for outcome in OUTCOMES + ("PRECONDITION_MISS", "HARNESS_ERROR")}


def _aggregate(key: tuple[str, str, str], records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = _empty_counts()
    latencies: list[float] = []
    resolved_latencies: list[float] = []
    critical_operation = CRITICAL_STEPS.get(key[2])
    for record in records:
        outcome = str(record.get("outcome") or "HARNESS_ERROR")
        counts[outcome] = counts.get(outcome, 0) + 1
        critical_operation = record.get("critical_operation") or critical_operation
        value = record.get("latency_ms")
        if outcome in OUTCOMES and isinstance(value, (int, float)) and value >= 0:
            latencies.append(float(value))
            if outcome in {"PASS", "VIOLATION"}:
                resolved_latencies.append(float(value))
    attempted = sum(counts[outcome] for outcome in OUTCOMES)
    decidable = counts["PASS"] + counts["VIOLATION"]
    return {
        "configuration_id": key[0],
        "scenario": key[1],
        "property": key[2],
        "history_count": len(records),
        "attempted": attempted,
        "PASS": counts["PASS"],
        "VIOLATION": counts["VIOLATION"],
        "UNAVAILABLE": counts["UNAVAILABLE"],
        "INDETERMINATE": counts["INDETERMINATE"],
        "PRECONDITION_MISS": counts["PRECONDITION_MISS"],
        "HARNESS_ERROR": counts["HARNESS_ERROR"],
        "violation_rate": _rate(counts["VIOLATION"], decidable),
        "definitive_completion_rate": _rate(decidable, attempted),
        "indeterminate_rate": _rate(counts["INDETERMINATE"], attempted),
        "critical_operation": critical_operation,
        "latency_n": len(latencies),
        "latency_missing": max(attempted - len(latencies), 0),
        "latency_status": (
            "NO_DATA"
            if not latencies
            else "COMPLETE"
            if len(latencies) == attempted
            else "PARTIAL"
        ),
        "p50_ms": quantile(latencies, 0.50),
        "p95_ms": quantile(latencies, 0.95),
        "resolved_latency_n": len(resolved_latencies),
        "resolved_latency_missing": max(decidable - len(resolved_latencies), 0),
        "resolved_latency_status": (
            "NO_DATA"
            if not resolved_latencies
            else "COMPLETE"
            if len(resolved_latencies) == decidable
            else "PARTIAL"
        ),
        "resolved_p50_ms": quantile(resolved_latencies, 0.50),
        "resolved_p95_ms": quantile(resolved_latencies, 0.95),
    }


def metric_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate one evidence row per configuration, scenario, and property."""

    grouped: defaultdict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        configuration = record.get("configuration_id")
        property_name = record.get("property")
        scenario = record.get("scenario")
        if all(isinstance(value, str) for value in (configuration, scenario, property_name)):
            grouped[(configuration, scenario, property_name)].append(record)
    return [_aggregate(key, grouped[key]) for key in sorted(grouped, key=_sort_key)]


def _metric_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {(row["configuration_id"], row["scenario"], row["property"]): row for row in rows}


def _difference(right: Any, left: Any) -> float | None:
    if isinstance(right, (int, float)) and isinstance(left, (int, float)):
        return float(right) - float(left)
    return None


def contrast_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build descriptive one-factor contrasts, retaining missing cells explicitly."""

    index = _metric_index(rows)
    scenarios = sorted(
        {row["scenario"] for row in rows},
        key=lambda scenario: SCENARIO_ORDER.index(scenario)
        if scenario in SCENARIO_ORDER
        else 99,
    )
    output: list[dict[str, Any]] = []
    for scenario in scenarios:
        for property_name in PROPERTIES:
            for left_config, right_config, factor in CONTRASTS:
                left = index.get((left_config, scenario, property_name))
                right = index.get((right_config, scenario, property_name))
                complete = left is not None and right is not None
                if not complete:
                    status = "MISSING_CELL"
                elif left["p95_ms"] is None and right["p95_ms"] is None:
                    status = "NO_LATENCY_DATA"
                elif left["p95_ms"] is None or right["p95_ms"] is None:
                    status = "LATENCY_PARTIAL"
                else:
                    status = "LATENCY_COMPLETE"
                output.append(
                    {
                        "scenario": scenario,
                        "property": property_name,
                        "left_config": left_config,
                        "right_config": right_config,
                        "factor": factor,
                        "cell_status": "CELL_PRESENT" if complete else "MISSING_CELL",
                        "status": status,
                        "left_history_count": left["history_count"] if left else None,
                        "right_history_count": right["history_count"] if right else None,
                        "left_violation_rate": left["violation_rate"] if left else None,
                        "right_violation_rate": right["violation_rate"] if right else None,
                        "delta_violation_rate": _difference(
                            right["violation_rate"] if right else None,
                            left["violation_rate"] if left else None,
                        ),
                        "left_definitive_completion_rate": (
                            left["definitive_completion_rate"] if left else None
                        ),
                        "right_definitive_completion_rate": (
                            right["definitive_completion_rate"] if right else None
                        ),
                        "delta_definitive_completion_rate": _difference(
                            right["definitive_completion_rate"] if right else None,
                            left["definitive_completion_rate"] if left else None,
                        ),
                        "left_p50_ms": left["p50_ms"] if left else None,
                        "right_p50_ms": right["p50_ms"] if right else None,
                        "delta_p50_ms": _difference(
                            right["p50_ms"] if right else None,
                            left["p50_ms"] if left else None,
                        ),
                        "left_p95_ms": left["p95_ms"] if left else None,
                        "right_p95_ms": right["p95_ms"] if right else None,
                        "delta_p95_ms": _difference(
                            right["p95_ms"] if right else None,
                            left["p95_ms"] if left else None,
                        ),
                    }
                )
    return output


def fault_delta_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare each recorded fault scenario with its RQ1 normal baseline."""

    index = _metric_index(rows)
    output: list[dict[str, Any]] = []
    fault_scenarios = [scenario for scenario in SCENARIO_ORDER if scenario != "normal"]
    for row in rows:
        scenario = row["scenario"]
        if scenario not in fault_scenarios:
            continue
        baseline = index.get((row["configuration_id"], "normal", row["property"]))
        if baseline is None:
            status = "MISSING_BASELINE"
        elif baseline["p95_ms"] is None and row["p95_ms"] is None:
            status = "NO_LATENCY_DATA"
        elif baseline["p95_ms"] is None or row["p95_ms"] is None:
            status = "LATENCY_PARTIAL"
        else:
            status = "LATENCY_COMPLETE"
        output.append(
            {
                "scenario": scenario,
                "configuration_id": row["configuration_id"],
                "property": row["property"],
                "status": status,
                "normal_history_count": baseline["history_count"] if baseline else None,
                "fault_history_count": row["history_count"],
                "normal_p95_ms": baseline["p95_ms"] if baseline else None,
                "fault_p95_ms": row["p95_ms"],
                "delta_p95_ms": _difference(row["p95_ms"], baseline["p95_ms"] if baseline else None),
                "normal_definitive_completion_rate": (
                    baseline["definitive_completion_rate"] if baseline else None
                ),
                "fault_definitive_completion_rate": row["definitive_completion_rate"],
                "delta_definitive_completion_rate": _difference(
                    row["definitive_completion_rate"],
                    baseline["definitive_completion_rate"] if baseline else None,
                ),
                "normal_violation_rate": baseline["violation_rate"] if baseline else None,
                "fault_violation_rate": row["violation_rate"],
                "delta_violation_rate": _difference(
                    row["violation_rate"], baseline["violation_rate"] if baseline else None
                ),
                "normal_indeterminate_rate": baseline["indeterminate_rate"] if baseline else None,
                "fault_indeterminate_rate": row["indeterminate_rate"],
                "delta_indeterminate_rate": _difference(
                    row["indeterminate_rate"],
                    baseline["indeterminate_rate"] if baseline else None,
                ),
            }
        )
    return output


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


METRIC_FIELDS = [
    "configuration_id",
    "scenario",
    "property",
    "history_count",
    "attempted",
    "PASS",
    "VIOLATION",
    "UNAVAILABLE",
    "INDETERMINATE",
    "PRECONDITION_MISS",
    "HARNESS_ERROR",
    "violation_rate",
    "definitive_completion_rate",
    "indeterminate_rate",
    "critical_operation",
    "latency_n",
    "latency_missing",
    "latency_status",
    "p50_ms",
    "p95_ms",
    "resolved_latency_n",
    "resolved_latency_missing",
    "resolved_latency_status",
    "resolved_p50_ms",
    "resolved_p95_ms",
]
CONTRAST_FIELDS = [
    "scenario",
    "property",
    "left_config",
    "right_config",
    "factor",
    "cell_status",
    "status",
    "left_history_count",
    "right_history_count",
    "left_violation_rate",
    "right_violation_rate",
    "delta_violation_rate",
    "left_definitive_completion_rate",
    "right_definitive_completion_rate",
    "delta_definitive_completion_rate",
    "left_p50_ms",
    "right_p50_ms",
    "delta_p50_ms",
    "left_p95_ms",
    "right_p95_ms",
    "delta_p95_ms",
]
FAULT_DELTA_FIELDS = [
    "scenario",
    "configuration_id",
    "property",
    "status",
    "normal_history_count",
    "fault_history_count",
    "normal_p95_ms",
    "fault_p95_ms",
    "delta_p95_ms",
    "normal_definitive_completion_rate",
    "fault_definitive_completion_rate",
    "delta_definitive_completion_rate",
    "normal_violation_rate",
    "fault_violation_rate",
    "delta_violation_rate",
    "normal_indeterminate_rate",
    "fault_indeterminate_rate",
    "delta_indeterminate_rate",
]


def _partition_counts(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, int]]:
    """Keep the four balanced partition signatures separate from other cells."""

    values = {
        signature: {outcome: 0 for outcome in OUTCOMES}
        for signature in PARTITION_SIGNATURES
    }
    for row in rows:
        if row.get("scenario") != "partition":
            continue
        signature = (row.get("configuration_id"), row.get("property"))
        if signature in values:
            for outcome in OUTCOMES:
                values[signature][outcome] += int(row.get(outcome, 0))
    return values


def generate_figures(figures_root: Path, rows: list[dict[str, Any]], contrasts: list[dict[str, Any]]) -> list[Path]:
    """Generate the three completion and latency figures as vector PDFs."""

    from .figures import generate_rq4_figures

    return generate_rq4_figures(figures_root, rows, contrasts)


def analyse(
    *,
    raw_root: Path = ROOT / "results/raw",
    summary_root: Path = ROOT / "results/summary/rq4",
    figures_root: Path = ROOT / "figures",
) -> dict[str, Any]:
    """Rebuild RQ4 CSVs and figures from immutable RQ1/RQ2 histories."""

    records = load_records(raw_root)
    rows = metric_rows(records)
    contrasts = contrast_rows(rows)
    deltas = fault_delta_rows(rows)
    _write_csv(summary_root / "metrics.csv", rows, METRIC_FIELDS)
    _write_csv(summary_root / "contrasts.csv", contrasts, CONTRAST_FIELDS)
    _write_csv(summary_root / "fault_deltas.csv", deltas, FAULT_DELTA_FIELDS)
    figure_paths = generate_figures(figures_root, rows, contrasts)
    summary = {
        "schema_version": "rq4-summary.v1",
        "status": "DATA" if records else "NO_DATA",
        "input_campaigns": sorted({record["campaign"] for record in records}),
        "history_count": len(records),
        "metric_row_count": len(rows),
        "contrast_row_count": len(contrasts),
        "fault_delta_row_count": len(deltas),
        "figures": [path.name for path in figure_paths],
        "definitions": {
            "violation_rate": "VIOLATION / (PASS + VIOLATION)",
            "definitive_completion_rate": "(PASS + VIOLATION) / attempted",
            "indeterminate_rate": "INDETERMINATE / attempted",
            "attempted": "PASS + VIOLATION + UNAVAILABLE + INDETERMINATE; precondition and harness failures are excluded",
            "all_attempt_latency": "critical operation end_ns - start_ns in milliseconds for every attempted outcome with valid timestamps; timeout outcomes are retained",
            "resolved_latency": "critical operation end_ns - start_ns in milliseconds for PASS and VIOLATION outcomes only",
            "availability_label": "observed definitive completion rate",
        },
    }
    (summary_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary
