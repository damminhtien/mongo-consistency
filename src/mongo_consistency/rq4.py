"""Offline RQ4 consistency, completion, and critical-operation latency analysis."""

from __future__ import annotations

import csv
import html
import json
import math
import shutil
import subprocess
import tempfile
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


def _svg_text(x: float, y: float, value: Any, *, size: int = 14, anchor: str = "start", color: str = "#172033") -> str:
    return (
        f'<text x="{x:g}" y="{y:g}" font-family="Arial,sans-serif" '
        f'font-size="{size}px" fill="{color}" text-anchor="{anchor}">{html.escape(str(value))}</text>'
    )


def _svg_document(title: str, body: str, width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>'
        f'{_svg_text(32, 38, title, size=22)}{body}</svg>'
    )


def _render_pdf(path: Path, title: str, body: str, width: int, height: int) -> None:
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise RuntimeError("rsvg-convert is required to render RQ4 PDF figures")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mongo-consistency-rq4-") as directory:
        svg_path = Path(directory) / "figure.svg"
        svg_path.write_text(_svg_document(title, body, width, height), encoding="utf-8")
        completed = subprocess.run(
            [converter, "-f", "pdf", "-o", str(path), str(svg_path)],
            capture_output=True,
            check=False,
            timeout=30,
            text=True,
        )
        if completed.returncode != 0 or not path.is_file():
            raise RuntimeError(f"rsvg-convert failed for {path}: {completed.stderr.strip()}")


def _partition_counts(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, int]]:
    """Return outcome counts for the balanced C1/C6 RYW and MW signatures."""

    values = {
        signature: {outcome: 0 for outcome in OUTCOMES}
        for signature in PARTITION_SIGNATURES
    }
    for row in rows:
        if row["scenario"] != "partition":
            continue
        signature = (row["configuration_id"], row["property"])
        if signature not in values:
            continue
        for outcome in OUTCOMES:
            values[signature][outcome] += int(row[outcome])
    return values


def _outcomes_partition_body(rows: list[dict[str, Any]]) -> tuple[str, int]:
    counts = _partition_counts(rows)
    colors = {
        "PASS": "#15803d",
        "VIOLATION": "#dc2626",
        "UNAVAILABLE": "#d97706",
        "INDETERMINATE": "#7c3aed",
    }
    left, top, bar_width, row_height = 190, 105, 660, 46
    body = _svg_text(
        left,
        68,
        "F3 partition cells; each bar is one C1/C6 RYW or MW comparison",
        size=13,
        color="#475569",
    )
    for offset, outcome in enumerate(OUTCOMES):
        x = left + offset * 165
        body += _svg_text(
            x + 70, 88, outcome, size=12, anchor="middle", color=colors[outcome]
        )
    max_total = max((sum(values.values()) for values in counts.values()), default=0) or 1
    for index, signature in enumerate(PARTITION_SIGNATURES):
        y = top + index * row_height
        configuration, property_name = signature
        body += _svg_text(
            left - 18,
            y + 22,
            f"{configuration} {property_name}",
            size=14,
            anchor="end",
        )
        total = sum(counts[signature].values())
        if not total:
            body += f'<rect x="{left}" y="{y + 3}" width="{bar_width}" height="24" fill="#f1f5f9" stroke="#cbd5e1"/>'
            body += _svg_text(left + bar_width / 2, y + 20, "no recorded cell", size=12, anchor="middle", color="#64748b")
            continue
        cursor = left
        for outcome in OUTCOMES:
            value = counts[signature][outcome]
            segment = bar_width * value / max_total
            if segment <= 0:
                continue
            body += f'<rect x="{cursor:g}" y="{y + 3}" width="{segment:g}" height="24" fill="{colors[outcome]}"/>'
            if segment >= 24:
                body += _svg_text(cursor + segment / 2, y + 20, value, size=11, anchor="middle", color="white")
            cursor += segment
        body += _svg_text(left + bar_width + 12, y + 20, f"n={total}", size=11, color="#475569")
    body += _svg_text(
        540,
        top + len(PARTITION_SIGNATURES) * row_height + 18,
        "Counts are not pooled across MR or WFR; unrecorded cells are left unfilled.",
        size=12,
        anchor="middle",
        color="#475569",
    )
    return body, top + len(PARTITION_SIGNATURES) * row_height + 45


def _latency_completion_body(rows: list[dict[str, Any]]) -> tuple[str, int]:
    partition_rows = [
        row
        for row in rows
        if row["scenario"] == "partition"
        and row["definitive_completion_rate"] is not None
    ]
    points = [row for row in partition_rows if row["p95_ms"] is not None]
    unresolved = [row for row in partition_rows if row["p95_ms"] is None]
    width, height = 1120, 760
    left, right, top, bottom = 145, 70, 90, 105
    plot_width, plot_height = width - left - right, height - top - bottom
    body = _svg_text(
        left,
        67,
        "Partition cells; client-observed time to the critical operation",
        size=13,
        color="#475569",
    )
    legend = (
        ("#dc2626", "VIOLATION"),
        ("#15803d", "decidable"),
        ("#64748b", "unresolved/no p95"),
    )
    for index, (color, label) in enumerate(legend):
        x = width - 315 + index * 105
        body += f'<circle cx="{x:g}" cy="65" r="5" fill="{color}"/>'
        body += _svg_text(x + 9, 69, label, size=10, color=color)
    body += f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#334155"/><line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#334155"/>'
    for tick in range(6):
        value = tick / 5
        y = top + plot_height * (1 - value)
        body += f'<line x1="{left - 5}" y1="{y:g}" x2="{left + plot_width}" y2="{y:g}" stroke="#e2e8f0"/>'
        body += _svg_text(left - 12, y + 5, f"{value:.1f}", size=11, anchor="end", color="#475569")
    max_latency = max((float(row["p95_ms"]) for row in points), default=1.0)
    max_latency = max(max_latency, 1.0)
    max_log_latency = math.log10(max_latency + 1)
    tick_values = [0.0, 1.0, 10.0, 100.0, 1000.0, max_latency]
    seen_ticks: set[float] = set()
    for tick_value in tick_values:
        if tick_value > max_latency or tick_value in seen_ticks:
            continue
        seen_ticks.add(tick_value)
        x = left + plot_width * math.log10(tick_value + 1) / max_log_latency
        body += f'<line x1="{x:g}" y1="{top + plot_height}" x2="{x:g}" y2="{top}" stroke="#e2e8f0"/>'
        body += _svg_text(x, top + plot_height + 18, f"{tick_value:g}", size=10, anchor="middle", color="#475569")
    top_rank = 0
    for row in points:
        x = left + plot_width * math.log10(float(row["p95_ms"]) + 1) / max_log_latency
        y = top + plot_height * (1 - float(row["definitive_completion_rate"]))
        decidable = int(row["PASS"]) + int(row["VIOLATION"])
        if int(row["VIOLATION"]) > 0:
            color = "#dc2626"
        elif decidable > 0:
            color = "#15803d"
        else:
            color = "#64748b"
        label = f'{row["configuration_id"]}-{row["property"]}'
        body += f'<circle cx="{x:g}" cy="{y:g}" r="6" fill="{color}"/>'
        if y <= top + 8:
            label_x = x + 9
            label_y = top + 20 + top_rank * 14
            top_rank += 1
        else:
            label_x = min(left + plot_width - 72, x + 9)
            label_y = y + 4
        body += _svg_text(label_x, label_y, label, size=11, color=color)
    unresolved_top = top + 20
    for index, row in enumerate(unresolved):
        x = left + plot_width - 130
        y = unresolved_top + index * 18
        label = f'{row["configuration_id"]}-{row["property"]} unresolved'
        body += f'<circle cx="{x:g}" cy="{y:g}" r="6" fill="#64748b"/>'
        body += _svg_text(x + 10, y + 4, label, size=10, color="#64748b")
    body += _svg_text(
        left + plot_width / 2,
        height - 48,
        f"client-observed p95 time to outcome (ms; all attempts; log scale; max shown {max_latency:.2f})",
        size=13,
        anchor="middle",
    )
    axis_y = top + plot_height / 2
    body += f'<g transform="rotate(-90 48 {axis_y:g})">{_svg_text(48, axis_y, "definitive completion rate", size=13, anchor="middle")}</g>'
    if not partition_rows:
        body += _svg_text(width / 2, height / 2, "no recorded partition cells", size=18, anchor="middle", color="#64748b")
    return body, height


def _contrast_body(contrasts: list[dict[str, Any]]) -> tuple[str, int]:
    rows = [
        row
        for row in contrasts
        if row["left_config"] == "C5"
        and row["right_config"] == "C6"
        and row["scenario"] in {"normal", "rq1_fault", "partition"}
    ]
    width, row_height = 1120, 36
    left, top, bar_width = 255, 95, 560
    body = _svg_text(left, 68, "Normal and property-specific fault schedules; C5 is absent from F3", size=13, color="#475569")
    body += _svg_text(left + bar_width / 2, 88, "critical-operation p95 latency (ms)", size=12, anchor="middle")
    max_value = max(
        (float(row[field]) for row in rows for field in ("left_p95_ms", "right_p95_ms") if row[field] is not None),
        default=1.0,
    )
    max_value = max(max_value, 1.0)
    visible = list(rows)
    for index, row in enumerate(visible):
        y = top + index * row_height
        label = f'{row["scenario"]}/{row["property"]}'
        body += _svg_text(left - 12, y + 17, label, size=11, anchor="end")
        if row["status"] != "LATENCY_COMPLETE":
            status_label = {
                "NO_LATENCY_DATA": "not compared",
                "MISSING_CELL": "not recorded",
            }.get(row["status"], row["status"].replace("_", " "))
            body += _svg_text(
                left,
                y + 17,
                status_label,
                size=11,
                color="#64748b",
            )
        for config, field, color in (("C5", "left_p95_ms", "#64748b"), ("C6", "right_p95_ms", "#2563eb")):
            value = row[field]
            if value is not None:
                bar = bar_width * float(value) / max_value
                body += f'<rect x="{left}" y="{y + (1 if config == "C5" else 18)}" width="{bar:g}" height="12" fill="{color}"/>'
                body += _svg_text(left + bar + 6, y + (11 if config == "C5" else 28), f"{config} {float(value):.2f}", size=10, color=color)
        delta = row.get("delta_p95_ms")
        completion = row.get("delta_definitive_completion_rate")
        body += _svg_text(left + bar_width + 170, y + 17, f"Delta L95={delta:.2f} ms" if delta is not None else "Delta L95=NA", size=10, color="#475569")
        body += _svg_text(left + bar_width + 170, y + 30, f"Delta D={completion:+.3f}" if completion is not None else "Delta D=NA", size=10, color="#475569")
    if not visible:
        body += _svg_text(width / 2, 210, "no recorded C5/C6 latency pairs", size=18, anchor="middle", color="#64748b")
    footer_y = top + max(len(visible), 1) * row_height + 36
    body += _svg_text(width / 2, footer_y, "Bars use the critical operation only; no p99 is reported for these small cells.", size=12, anchor="middle", color="#475569")
    return body, footer_y + 30


def generate_figures(figures_root: Path, rows: list[dict[str, Any]], contrasts: list[dict[str, Any]]) -> list[Path]:
    """Generate the three completion and latency figures from aggregate rows."""

    figures_root.mkdir(parents=True, exist_ok=True)
    outputs = [
        (figures_root / "rq4_outcomes_partition.pdf", _outcomes_partition_body(rows), "Paired partition signature outcomes"),
        (figures_root / "rq4_latency_completion.pdf", _latency_completion_body(rows), "Client-observed time versus completion"),
        (figures_root / "rq4_c5_c6_contrast.pdf", _contrast_body(contrasts), "C5 versus C6 contrast"),
    ]
    paths: list[Path] = []
    for path, (body, height), title in outputs:
        _render_pdf(path, title, body, 1120, height)
        paths.append(path)
    return paths


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
