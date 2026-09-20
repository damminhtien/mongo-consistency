"""Deterministic offline summaries and figures from raw histories."""

from __future__ import annotations

import csv
import html
import json
import math
import shutil
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .checkers import check_history
from .config import CONFIG_ROOT, load_configurations, load_predictions
from .history import read_history
from .models import History, Outcome

MR_RERUN_RELATIVE_PATH = Path("mr-rerun/experiment")
MR_RERUN_HISTORY_COUNT = 320


def quantile(values: Iterable[float], probability: float) -> float | None:
    """Return a linear-interpolated quantile with stable empty handling."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _duration_ms(history: History) -> list[float]:
    return [
        (operation.end_ns - operation.start_ns) / 1_000_000
        for operation in history.operations
        if operation.kind != "setup"
        and operation.start_ns is not None
        and operation.end_ns is not None
        and operation.end_ns >= operation.start_ns
    ]


def _operation_metrics(history: History) -> dict[str, Any]:
    operations = [operation for operation in history.operations if operation.kind != "setup"]
    successful = sum(operation.operation_status == "SUCCESS" for operation in operations)
    attempted = len(operations)
    durations = _duration_ms(history)
    return {
        "attempted": attempted,
        "successful": successful,
        "success_rate": successful / attempted if attempted else None,
        "latencies_ms": durations,
        "latency_ms": {
            "p50": quantile(durations, 0.50),
            "p95": quantile(durations, 0.95),
            "p99": quantile(durations, 0.99),
        },
    }


def _event_durations(history: History) -> dict[str, list[float]]:
    """Extract election and recovery intervals from recorded fault events."""

    election: list[float] = []
    recovery: list[float] = []
    for event in history.fault_events:
        election_start = event.get("election_start_ns")
        election_end = event.get("election_end_ns")
        if isinstance(election_start, int) and isinstance(election_end, int) and election_end >= election_start:
            election.append((election_end - election_start) / 1_000_000)
        recovery_start = event.get("recovery_start_ns")
        recovery_end = event.get("recovery_end_ns")
        if isinstance(recovery_start, int) and isinstance(recovery_end, int) and recovery_end >= recovery_start:
            recovery.append((recovery_end - recovery_start) / 1_000_000)
    return {"election_ms": election, "recovery_ms": recovery}


def _quantile_set(values: Iterable[float]) -> dict[str, float | None]:
    values_list = list(values)
    return {
        "p50": quantile(values_list, 0.50),
        "p95": quantile(values_list, 0.95),
        "p99": quantile(values_list, 0.99),
    }


def _trace(history: History) -> list[dict[str, Any]]:
    """Return compact operation fields for a representative-trace figure."""

    return [
        {
            "operation_id": operation.operation_id,
            "kind": operation.kind,
            "status": operation.operation_status,
            "requested_member": operation.requested_member,
            "actual_server_address": operation.actual_server_address,
            "actual_role": operation.actual_role,
            "causal_session": operation.causal_session,
            "read_concern": operation.read_concern,
            "write_concern": operation.write_concern,
            "after_cluster_time": operation.after_cluster_time,
            "operation_time_after": operation.operation_time_after,
            "error_code": operation.error_code,
            "response_received": operation.response_received,
            "command_started": operation.command_started,
            "duration_ms": (
                (operation.end_ns - operation.start_ns) / 1_000_000
                if operation.start_ns is not None
                and operation.end_ns is not None
                and operation.end_ns >= operation.start_ns
                else None
            ),
        }
        for operation in history.operations
    ]


def _history_row(path: Path, raw_root: Path) -> dict[str, Any]:
    relative = path.relative_to(raw_root).as_posix()
    try:
        history = read_history(path)
    except Exception as error:  # noqa: BLE001  # Malformed input is visible as a row.
        return {
            "path": relative,
            "outcome": Outcome.HARNESS_ERROR.value,
            "reason": f"history could not be loaded: {error}",
            "configuration_id": None,
            "property": None,
            "campaign_id": None,
            "adversarial": None,
            "operation_metrics": {
                "attempted": 0,
                "successful": 0,
                "success_rate": None,
                "latencies_ms": [],
                "latency_ms": {},
            },
            "event_metrics": {"election_ms": [], "recovery_ms": []},
            "trace": [],
        }
    result = check_history(history)
    manifest = history.manifest
    operations = _operation_metrics(history)
    return {
        "path": relative,
        "history_hash": history.history_hash,
        "trial_id": manifest.get("trial_id"),
        "outcome": result.outcome.value,
        "reason": result.reason,
        "details": result.details,
        "configuration_id": manifest.get("configuration_id"),
        "property": manifest.get("property"),
        "campaign_id": manifest.get("campaign_id"),
        "runner_commit": manifest.get("runner_commit"),
        "adversarial": manifest.get("adversarial", False),
        "seed": manifest.get("seed"),
        "operation_metrics": operations,
        "event_metrics": _event_durations(history),
        "trace": _trace(history),
        "fault_events": [
            {
                "action": event.get("action"),
                "event_id": event.get("event_id"),
                "members": event.get("members", []),
                "status": event.get("status"),
                "applied_ns": event.get("applied_ns"),
                "stable_topology": {
                    key: event["stable_topology"].get(key)
                    for key in ("stable", "primary", "secondaries")
                }
                if isinstance(event.get("stable_topology"), dict)
                else {},
            }
            for event in history.fault_events
        ],
        "prediction_manifest_hash": manifest.get("prediction_manifest_hash"),
    }


def _merge_mr_rerun_rows(
    base_rows: list[dict[str, Any]],
    replacement_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    expected_count: int = MR_RERUN_HISTORY_COUNT,
) -> list[dict[str, Any]]:
    """Replace the original MR slice only after a complete identity-checked rerun."""

    if (
        manifest.get("campaign") != "experiment"
        or manifest.get("status") != "COMPLETE"
        or manifest.get("expected_case_count") != expected_count
        or manifest.get("case_count") != expected_count
        or manifest.get("completed_case_count") != expected_count
    ):
        raise ValueError("MR rerun manifest is incomplete or does not match the registered plan")

    planned_ordinals = manifest.get("planned_ordinals")
    if (
        not isinstance(planned_ordinals, list)
        or len(planned_ordinals) != expected_count
        or any(not isinstance(value, int) or isinstance(value, bool) for value in planned_ordinals)
        or len(set(planned_ordinals)) != expected_count
    ):
        raise ValueError("MR rerun manifest has an invalid ordinal plan")
    records = manifest.get("records")
    if (
        not isinstance(records, list)
        or len(records) != expected_count
        or any(not isinstance(record, dict) for record in records)
    ):
        raise ValueError("MR rerun manifest does not contain all completed records")

    base_mr_rows = {
        row.get("trial_id"): row
        for row in base_rows
        if row.get("campaign_id") == "experiment" and row.get("property") == "MR"
    }
    replacement_by_id = {row.get("trial_id"): row for row in replacement_rows}
    records_by_id = {record.get("trial_id"): record for record in records}
    expected_ids = set(base_mr_rows)
    if (
        len(base_mr_rows) != expected_count
        or len(replacement_by_id) != expected_count
        or len(records_by_id) != expected_count
        or set(replacement_by_id) != expected_ids
        or set(records_by_id) != expected_ids
    ):
        raise ValueError("MR rerun cases do not exactly match the original MR histories")

    for trial_id, replacement in replacement_by_id.items():
        original = base_mr_rows[trial_id]
        record = records_by_id[trial_id]
        if replacement.get("campaign_id") != "experiment" or replacement.get("property") != "MR":
            raise ValueError(f"MR rerun contains a non-MR history: {trial_id}")
        for field in ("configuration_id", "property", "adversarial", "seed"):
            if replacement.get(field) != original.get(field):
                raise ValueError(f"MR rerun changed {field} for {trial_id}")
            if record.get(field) != replacement.get(field):
                raise ValueError(f"MR rerun manifest disagrees on {field} for {trial_id}")
        if record.get("history_hash") != replacement.get("history_hash"):
            raise ValueError(f"MR rerun manifest hash does not match history {trial_id}")

    return [
        replacement_by_id.get(row.get("trial_id"), row)
        if row.get("campaign_id") == "experiment" and row.get("property") == "MR"
        else row
        for row in base_rows
    ]


def load_rows(raw_root: Path) -> list[dict[str, Any]]:
    """Load all canonical trial JSON files below a raw-results root."""

    if not raw_root.exists():
        return []
    paths = sorted(
        path
        for path in raw_root.rglob("*.json")
        if path.name != "campaign-manifest.json"
    )
    rerun_root = raw_root / MR_RERUN_RELATIVE_PATH
    rerun_paths = [path for path in paths if path.is_relative_to(rerun_root)]
    base_paths = [path for path in paths if not path.is_relative_to(rerun_root)]
    base_rows = [_history_row(path, raw_root) for path in base_paths]
    if not rerun_root.exists():
        return base_rows

    manifest_path = rerun_root / "campaign-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"MR rerun directory has no campaign manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read MR rerun manifest {manifest_path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError(f"MR rerun manifest is not an object: {manifest_path}")
    replacement_rows = [_history_row(path, raw_root) for path in rerun_paths]
    if len(replacement_rows) != MR_RERUN_HISTORY_COUNT:
        raise ValueError(
            f"MR rerun contains {len(replacement_rows)} histories; "
            f"expected {MR_RERUN_HISTORY_COUNT}"
        )
    return _merge_mr_rerun_rows(base_rows, replacement_rows, manifest)


def _counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(row.get("outcome", Outcome.HARNESS_ERROR.value) for row in rows)
    return {outcome.value: counter.get(outcome.value, 0) for outcome in Outcome}


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _group_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = _counts(rows)
    operation_attempted = sum(row["operation_metrics"]["attempted"] for row in rows)
    operation_successful = sum(row["operation_metrics"]["successful"] for row in rows)
    latency_values = [
        value
        for row in rows
        for value in row["operation_metrics"].get("latencies_ms", [])
    ]
    election_values = [
        value
        for row in rows
        for value in row.get("event_metrics", {}).get("election_ms", [])
    ]
    recovery_values = [
        value
        for row in rows
        for value in row.get("event_metrics", {}).get("recovery_ms", [])
    ]
    decidable = counts[Outcome.PASS.value] + counts[Outcome.VIOLATION.value]
    completed = sum(
        counts[outcome.value]
        for outcome in (
            Outcome.PASS,
            Outcome.VIOLATION,
            Outcome.UNAVAILABLE,
            Outcome.INDETERMINATE,
        )
    )
    return {
        "history_count": len(rows),
        "outcomes": counts,
        "consistency_violation_rate": _rate(counts[Outcome.VIOLATION.value], decidable),
        "operation_success_rate": _rate(operation_successful, operation_attempted),
        "history_completion_rate": _rate(completed, len(rows)),
        "latency_ms": _quantile_set(latency_values),
        "election_ms": _quantile_set(election_values),
        "recovery_ms": _quantile_set(recovery_values),
    }


def group_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate by campaign, configuration, property, and fault mode."""

    grouped: defaultdict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("campaign_id"),
            bool(row.get("adversarial", False)),
            row.get("configuration_id"),
            row.get("property"),
        )
        grouped[key].append(row)
    result: list[dict[str, Any]] = []
    for (campaign, adversarial, configuration_id, property_name), members in sorted(
        grouped.items(), key=lambda item: tuple("" if value is None else str(value) for value in item[0])
    ):
        result.append(
            {
                "campaign_id": campaign,
                "adversarial": adversarial,
                "configuration_id": configuration_id,
                "property": property_name,
                **_group_summary(members),
            }
        )
    return result


def campaign_summaries(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep pilot, control, and adversarial outcomes in separate aggregates."""

    campaign_ids = sorted(
        {str(row["campaign_id"]) for row in rows if row.get("campaign_id") is not None}
    )
    campaigns: dict[str, dict[str, Any]] = {}
    for campaign_id in campaign_ids:
        campaign_rows = [row for row in rows if row.get("campaign_id") == campaign_id]
        normal_rows = [row for row in campaign_rows if not row.get("adversarial", False)]
        adversarial_rows = [row for row in campaign_rows if row.get("adversarial", False)]
        properties = sorted(
            {
                str(row["property"])
                for row in adversarial_rows
                if row.get("property") is not None
            }
        )
        campaigns[campaign_id] = {
            "history_count": len(campaign_rows),
            "outcomes": _counts(campaign_rows),
            "overall": _group_summary(campaign_rows),
            "normal": _group_summary(normal_rows),
            "adversarial": _group_summary(adversarial_rows),
            "properties": {
                property_name: _group_summary(
                    [
                        row
                        for row in adversarial_rows
                        if row.get("property") == property_name
                    ]
                )
                for property_name in properties
            },
        }
    return campaigns


def _mean(values: Iterable[float]) -> float | None:
    values_list = list(values)
    return sum(values_list) / len(values_list) if values_list else None


def _factorial_contrast(
    cells: dict[str, float | None],
    configurations: dict[str, dict[str, Any]],
    factors: tuple[str, ...],
) -> float | None:
    signed: list[tuple[int, float]] = []
    for configuration_id, value in cells.items():
        configuration = configurations.get(configuration_id)
        if configuration is None or value is None:
            continue
        sign = 1
        for factor in factors:
            raw = configuration["causal_session"] if factor == "causal_session" else configuration[factor]
            sign *= 1 if (raw is True or raw == "majority") else -1
        signed.append((sign, value))
    plus = _mean(value for sign, value in signed if sign > 0)
    minus = _mean(value for sign, value in signed if sign < 0)
    if plus is None or minus is None:
        return None
    return plus - minus


def factorial_analysis(
    summaries: list[dict[str, Any]],
    configurations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compute factorial contrasts for each main adversarial metric."""

    selected = [
        summary
        for summary in summaries
        if summary["campaign_id"] == "experiment" and summary["adversarial"]
    ]
    by_property: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for summary in selected:
        by_property[str(summary["property"])].append(summary)
    factors = {
        "read_concern": "read_concern",
        "write_concern": "write_concern",
        "causal_session": "causal_session",
    }
    interactions = {
        "read_concern_x_causal_session": ("read_concern", "causal_session"),
        "write_concern_x_causal_session": ("write_concern", "causal_session"),
        "read_concern_x_write_concern": ("read_concern", "write_concern"),
        "read_concern_x_write_concern_x_causal_session": (
            "read_concern",
            "write_concern",
            "causal_session",
        ),
    }
    metric_getters = {
        "consistency_violation_rate": lambda summary: summary["consistency_violation_rate"],
        "operation_success_rate": lambda summary: summary["operation_success_rate"],
        "history_completion_rate": lambda summary: summary["history_completion_rate"],
        "latency_p50_ms": lambda summary: summary["latency_ms"]["p50"],
        "election_p50_ms": lambda summary: summary["election_ms"]["p50"],
        "recovery_p50_ms": lambda summary: summary["recovery_ms"]["p50"],
    }
    output: dict[str, Any] = {"status": "NO_DATA", "properties": {}}
    for property_name, property_summaries in sorted(by_property.items()):
        metric_cells = {
            metric: {
                str(summary["configuration_id"]): getter(summary)
                for summary in property_summaries
            }
            for metric, getter in metric_getters.items()
        }
        consistency_cells = metric_cells["consistency_violation_rate"]
        property_output = {
            "cell_values": consistency_cells,
            "main_effects": {
                name: _factorial_contrast(consistency_cells, configurations, (factor,))
                for name, factor in factors.items()
            },
            "interactions": {
                name: _factorial_contrast(consistency_cells, configurations, factor_tuple)
                for name, factor_tuple in interactions.items()
            },
            "metrics": {
                metric: {
                    "cell_values": cells,
                    "main_effects": {
                        name: _factorial_contrast(cells, configurations, (factor,))
                        for name, factor in factors.items()
                    },
                    "interactions": {
                        name: _factorial_contrast(cells, configurations, factor_tuple)
                        for name, factor_tuple in interactions.items()
                    },
                }
                for metric, cells in metric_cells.items()
            },
            "delta_formulas": {
                "Delta_CS": "Y(rc,wc,on) - Y(rc,wc,off)",
                "Delta_RC": "Y(majority,wc,cs) - Y(local,wc,cs)",
                "Delta_WC": "Y(rc,majority,cs) - Y(rc,w:1,cs)",
            },
        }
        if any(value is not None for value in consistency_cells.values()):
            output["status"] = "DATA"
        output["properties"][property_name] = property_output
    return output


def _svg(path: Path, title: str, body: str, *, height: int = 420) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}">
<defs>
  <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
    <path d="M0,0 L8,4 L0,8 z" fill="#334155"/>
  </marker>
</defs>
<rect width="100%" height="100%" fill="white"/>
<text x="40" y="42" font-family="sans-serif" font-size="22" font-weight="bold">{html.escape(title)}</text>
{body}
</svg>
'''
    path.write_text(content, encoding="utf-8")


def _svg_text(
    x: float,
    y: float,
    value: object,
    *,
    size: int = 15,
    anchor: str = "start",
    weight: str = "normal",
    color: str = "#111827",
) -> str:
    return (
        f'<text x="{x:g}" y="{y:g}" font-family="sans-serif" font-size="{size}px" '
        f'text-anchor="{anchor}" font-weight="{weight}" fill="{color}">{html.escape(str(value))}</text>'
    )


def _svg_box(x: float, y: float, width: float, height: float, label: str, fill: str = "#e2e8f0") -> str:
    return (
        f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" '
        f'rx="8" fill="{fill}" stroke="#475569"/>'
        + _svg_text(x + width / 2, y + height / 2 + 5, label, anchor="middle", size=14)
    )


def _svg_arrow(x1: float, y1: float, x2: float, y2: float, label: str | None = None) -> str:
    body = f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" stroke="#334155" marker-end="url(#arrow)"/>'
    if label:
        body += _svg_text((x1 + x2) / 2, (y1 + y2) / 2 - 8, label, anchor="middle", size=12, color="#475569")
    return body


def _empty_figure(title: str, message: str) -> tuple[str, int]:
    return _svg_text(500, 190, "NO_DATA", size=24, anchor="middle", weight="bold") + _svg_text(500, 225, message, anchor="middle"), 300


def _timeline_body() -> tuple[str, int]:
    rows = (
        ("RYW", ("W(x,v1)", "R(x)")),
        ("MR", ("R1(x)", "R2(x)")),
        ("MW", ("W1(x,v1)", "partition + election", "W2(x,v2)", "snapshot(x)")),
        ("WFR", ("R1(x,v1)", "partition + election", "W2(x,v2)", "snapshot(x)")),
    )
    body = ""
    for row_index, (name, labels) in enumerate(rows):
        y = 75 + row_index * 75
        body += _svg_text(45, y + 28, name, size=16, weight="bold")
        x = 120
        for label_index, label in enumerate(labels):
            width = 155 if "partition" in label else 125
            fill = "#fee2e2" if "partition" in label else "#dbeafe"
            body += _svg_box(x, y, width, 42, label, fill)
            if label_index < len(labels) - 1:
                body += _svg_arrow(x + width, y + 21, x + width + 28, y + 21)
            x += width + 35
    return body, 390


def _architecture_body() -> tuple[str, int]:
    body = _svg_box(45, 125, 170, 55, "runner", "#dcfce7")
    body += _svg_box(330, 75, 180, 55, "mongo1 / primary", "#dbeafe")
    body += _svg_box(330, 155, 180, 55, "mongo2 / secondary", "#dbeafe")
    body += _svg_box(330, 235, 180, 55, "mongo3 / secondary", "#dbeafe")
    body += _svg_box(700, 125, 210, 55, "fault controllers", "#fee2e2")
    body += _svg_arrow(215, 145, 330, 102, "client_net")
    body += _svg_arrow(215, 152, 330, 182)
    body += _svg_arrow(215, 160, 330, 262)
    body += _svg_arrow(510, 102, 700, 145, "replica_net control")
    body += _svg_arrow(510, 182, 700, 152)
    body += _svg_arrow(510, 262, 700, 160)
    body += _svg_text(500, 355, "Client access is retained while replica-path traffic is controlled.", anchor="middle", color="#475569")
    return body, 400


def _fault_topology_body() -> tuple[str, int]:
    body = _svg_text(90, 85, "Normal", size=17, weight="bold")
    body += _svg_box(70, 110, 125, 45, "primary", "#dcfce7")
    body += _svg_box(250, 110, 125, 45, "secondary", "#dbeafe")
    body += _svg_box(430, 110, 125, 45, "secondary", "#dbeafe")
    body += _svg_arrow(195, 132, 250, 132)
    body += _svg_arrow(375, 132, 430, 132)
    body += _svg_text(90, 225, "RYW / MR", size=17, weight="bold")
    body += _svg_box(70, 250, 125, 45, "primary", "#dcfce7")
    body += _svg_box(250, 250, 125, 45, "stale", "#fee2e2")
    body += _svg_box(430, 250, 125, 45, "fresh", "#dbeafe")
    body += _svg_text(312, 315, "one secondary replication path blocked", anchor="middle", size=13, color="#991b1b")
    body += _svg_text(650, 85, "MW / WFR", size=17, weight="bold")
    body += _svg_box(630, 110, 125, 45, "old primary", "#fee2e2")
    body += _svg_box(810, 110, 125, 45, "new primary", "#dcfce7")
    body += _svg_arrow(755, 132, 810, 132, "election")
    body += _svg_text(782, 205, "old side isolated", anchor="middle", size=13, color="#991b1b")
    return body, 340


def _summary_for(summaries: list[dict[str, Any]], configuration_id: str, property_name: str) -> dict[str, Any] | None:
    candidates = [
        summary
        for summary in summaries
        if summary.get("configuration_id") == configuration_id
        and summary.get("property") == property_name
    ]
    adversarial = [summary for summary in candidates if summary.get("campaign_id") == "experiment" and summary.get("adversarial")]
    return (adversarial or candidates)[0] if (adversarial or candidates) else None


def _heatmap_body(summaries: list[dict[str, Any]]) -> tuple[str, int]:
    properties = ("RYW", "MR", "MW", "WFR")
    body = ""
    left = 150
    top = 80
    cell_width = 180
    cell_height = 35
    for column, property_name in enumerate(properties):
        body += _svg_text(left + column * cell_width + cell_width / 2, top, property_name, anchor="middle", weight="bold")
    for row, configuration_id in enumerate(f"C{number}" for number in range(1, 9)):
        y = top + 12 + row * cell_height
        body += _svg_text(125, y + 23, configuration_id, anchor="end", weight="bold")
        for column, property_name in enumerate(properties):
            summary = _summary_for(summaries, configuration_id, property_name)
            counts = summary.get("outcomes", {}) if summary else {}
            decidable = int(counts.get("PASS", 0)) + int(counts.get("VIOLATION", 0))
            violations = int(counts.get("VIOLATION", 0))
            rate = violations / decidable if decidable else None
            if rate is None:
                fill, label = "#f1f5f9", "NA"
            else:
                red = 255
                green = max(80, int(220 - 120 * rate))
                fill, label = f"rgb({red},{green},{green})", f"{violations}/{decidable}"
            x = left + column * cell_width
            body += f'<rect x="{x:g}" y="{y:g}" width="{cell_width - 8:g}" height="{cell_height - 5:g}" fill="{fill}" stroke="#cbd5e1"/>'
            body += _svg_text(x + (cell_width - 8) / 2, y + 20, label, anchor="middle", size=13)
    body += _svg_text(500, 390, "Cell label: VIOLATION / (PASS + VIOLATION); NA means no decidable history.", anchor="middle", size=13, color="#475569")
    return body, 420


def _factorial_body(factorial: dict[str, Any]) -> tuple[str, int]:
    properties = factorial.get("properties", {})
    if not properties:
        return _empty_figure("Factorial interactions", "No experiment adversarial summaries are available." )
    labels = ("RC", "WC", "CS", "RCxCS", "WCxCS", "RCxWC", "RCxWCxCS")
    keys = (
        "read_concern",
        "write_concern",
        "causal_session",
        "read_concern_x_causal_session",
        "write_concern_x_causal_session",
        "read_concern_x_write_concern",
        "read_concern_x_write_concern_x_causal_session",
    )
    body = ""
    for row, property_name in enumerate(("RYW", "MR", "MW", "WFR")):
        property_data = properties.get(property_name)
        if not property_data:
            continue
        effects = property_data.get("main_effects", {}) | property_data.get("interactions", {})
        y = 80 + row * 75
        body += _svg_text(42, y + 20, property_name, size=15, weight="bold")
        for index, (label, key) in enumerate(zip(labels, keys, strict=True)):
            value = effects.get(key)
            numeric = float(value) if isinstance(value, (int, float)) else 0.0
            height = min(42.0, max(2.0, abs(numeric) * 42)) if value is not None else 2.0
            x = 105 + index * 120
            base = y + 45
            fill = "#2563eb" if numeric >= 0 else "#dc2626"
            body += f'<rect x="{x:g}" y="{base - height:g}" width="75" height="{height:g}" fill="{fill}" opacity="0.85"/>'
            body += _svg_text(x + 37.5, y + 65, label, anchor="middle", size=11)
            body += _svg_text(x + 37.5, base - height - 5, f"{numeric:.3f}" if value is not None else "NA", anchor="middle", size=10)
    body += _svg_text(500, 390, "Blue is a positive contrast; red is a negative contrast for the checked metric.", anchor="middle", size=13, color="#475569")
    return body, 420


def _latency_body(
    normal_summary: dict[str, Any], adversarial_summary: dict[str, Any]
) -> tuple[str, int]:
    normal_latency = normal_summary.get("latency_ms", {})
    adversarial_latency = adversarial_summary.get("latency_ms", {})
    values = [
        (label, normal_latency.get(label), adversarial_latency.get(label))
        for label in ("p50", "p95", "p99")
    ]
    if not any(normal is not None or adversarial is not None for _, normal, adversarial in values):
        return _empty_figure("Operation latency", "No operation timings are available.")
    maximum = max(
        math.log10(float(value) + 1.0)
        for _, normal, adversarial in values
        for value in (normal, adversarial)
        if value is not None
    ) or 1.0
    body = ""
    for index, (label, normal, adversarial) in enumerate(values):
        group_x = 160 + index * 280
        for offset, value, color in (
            (0, normal, "#2563eb"),
            (84, adversarial, "#ea580c"),
        ):
            height = 190 * math.log10(float(value or 0) + 1.0) / maximum
            x = group_x + offset
            body += f'<rect x="{x:g}" y="{290 - height:g}" width="64" height="{height:g}" fill="{color}"/>'
            if value is not None:
                body += _svg_text(x + 32, max(88, 282 - height), f"{float(value):.2f}", anchor="middle", size=10)
        body += _svg_text(group_x + 74, 316, label, anchor="middle", weight="bold")
    body += '<rect x="350" y="350" width="14" height="14" fill="#2563eb"/>'
    body += _svg_text(370, 362, "Normal control", size=12)
    body += '<rect x="535" y="350" width="14" height="14" fill="#ea580c"/>'
    body += _svg_text(555, 362, "Adversarial", size=12)
    body += _svg_text(500, 397, "Main campaign only; bar heights use log10(ms + 1); pilot excluded.", anchor="middle", size=13, color="#475569")
    return body, 420


def _trace_body(rows: list[dict[str, Any]]) -> tuple[str, int]:
    def is_causal_timeout(candidate: dict[str, Any]) -> bool:
        operations = {
            item.get("operation_id"): item
            for item in candidate.get("trace", [])
        }
        write = operations.get("write", {})
        read = operations.get("read", {})
        return (
            candidate.get("configuration_id") == "C6"
            and candidate.get("property") == "RYW"
            and candidate.get("adversarial") is True
            and candidate.get("outcome") == Outcome.UNAVAILABLE.value
            and write.get("status") == "SUCCESS"
            and write.get("write_concern") == "majority"
            and read.get("status") == Outcome.UNAVAILABLE.value
            and read.get("causal_session") is True
            and read.get("read_concern") == "majority"
            and read.get("after_cluster_time") == write.get("operation_time_after")
            and read.get("error_code") == "NetworkTimeout"
            and read.get("response_received") is False
        )

    row = next(
        (candidate for candidate in rows if is_causal_timeout(candidate)),
        None,
    )
    if row is not None:
        operations = {item.get("operation_id"): item for item in row["trace"]}
        write = operations["write"]
        read = operations["read"]
        isolate = next(
            (event for event in row.get("fault_events", []) if event.get("action") == "isolate"),
            {},
        )
        heal = next(
            (event for event in row.get("fault_events", []) if event.get("action") == "heal"),
            {},
        )
        after_cluster_time = read.get("after_cluster_time") or {}
        after_label = (
            f"t={after_cluster_time.get('seconds')}, i={after_cluster_time.get('increment')}"
        )
        duration = read.get("duration_ms")
        duration_label = f"{float(duration) / 1000:.2f} s" if duration is not None else "timeout"
        fault_member = next(iter(isolate.get("members", [])), "secondary")
        stable = heal.get("stable_topology", {}).get("stable") is True
        path = row.get("path", "raw history")
        history_hash = str(row.get("history_hash", ""))
        body = _svg_text(45, 75, f"{path}  outcome={row.get('outcome')}", size=13, color="#475569")
        body += _svg_text(45, 96, f"history SHA-256: {history_hash}", size=12, color="#475569")
        entries = (
            (
                "1  ISOLATE",
                f"replication path on {fault_member}; event={isolate.get('status', 'unknown')}",
                "#dbeafe",
            ),
            (
                "2  W1",
                f"writeConcern=majority; {write.get('actual_server_address')} "
                f"({write.get('actual_role')}); {write.get('status')}",
                "#dcfce7",
            ),
            (
                "3  R1",
                f"readConcern=majority; causal=ON; {read.get('actual_server_address')} "
                f"({read.get('actual_role')}); afterClusterTime=({after_label})",
                "#fef3c7",
            ),
            (
                "4  RESULT",
                f"{read.get('error_code')} after {duration_label}; no response or read value; UNAVAILABLE",
                "#fee2e2",
            ),
            (
                "5  HEAL",
                f"event={heal.get('status', 'unknown')}; stable topology restored={stable}",
                "#e2e8f0",
            ),
        )
        for index, (label, description, fill) in enumerate(entries):
            y = 116 + index * 46
            body += _svg_box(42, y, 130, 34, label, fill)
            body += _svg_text(190, y + 22, description, size=13)
        body += _svg_text(
            45,
            361,
            "The client reached the isolated secondary, but the read returned no document.",
            size=13,
            color="#475569",
        )
        return body, 385

    row = next((candidate for candidate in rows if candidate.get("trace")), None)
    if row is None:
        return _empty_figure("Representative trace", "No completed operation trace is available.")
    body = _svg_text(45, 78, f"{row.get('path')}  hash={str(row.get('history_hash', ''))[:16]}", size=13, color="#475569")
    columns = ((45, "operation"), (185, "kind"), (290, "requested"), (430, "actual"), (650, "role"), (780, "status"))
    for x, label in columns:
        body += _svg_text(x, 112, label, size=13, weight="bold")
    for index, operation in enumerate(row["trace"][:7]):
        y = 145 + index * 32
        values = (
            operation.get("operation_id"), operation.get("kind"), operation.get("requested_member"),
            operation.get("actual_server_address"), operation.get("actual_role"), operation.get("status"),
        )
        for (x, _label), value in zip(columns, values, strict=True):
            body += _svg_text(x, y, value or "NA", size=12)
    return body, 390


def _prediction_body(
    predictions: dict[str, dict[str, Any]], summaries: list[dict[str, Any]],
) -> tuple[str, int]:
    properties = ("RYW", "MR", "MW", "WFR")
    body = ""
    left = 120
    cell_width = 170
    for column, property_name in enumerate(properties):
        body += _svg_text(left + column * cell_width + 70, 80, property_name, anchor="middle", weight="bold")
    for row, configuration_id in enumerate(f"C{number}" for number in range(1, 9)):
        y = 98 + row * 34
        body += _svg_text(95, y + 21, configuration_id, anchor="end", weight="bold")
        target = set(predictions.get(configuration_id, {}).get("guarantee_targets", []))
        for column, property_name in enumerate(properties):
            summary = _summary_for(summaries, configuration_id, property_name)
            label = "NA"
            if summary:
                counts = summary.get("outcomes", {})
                if counts.get("VIOLATION", 0):
                    label = "VIOLATION"
                elif counts.get("PASS", 0):
                    label = "PASS"
                elif counts.get("UNAVAILABLE", 0):
                    label = "UNAVAILABLE"
                elif counts.get("INDETERMINATE", 0):
                    label = "INDETERMINATE"
            x = left + column * cell_width
            fill = "#dcfce7" if property_name in target else "#f1f5f9"
            body += f'<rect x="{x:g}" y="{y:g}" width="140" height="28" fill="{fill}" stroke="#cbd5e1"/>'
            body += _svg_text(x + 70, y + 19, label, anchor="middle", size=11)
    body += _svg_text(500, 390, "Cell shading marks the documented guarantee target; labels come from checked histories.", anchor="middle", size=13, color="#475569")
    return body, 420


def generate_figures(
    figures_root: Path,
    summaries: list[dict[str, Any]],
    factorial: dict[str, Any],
    rows: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]] | None = None,
) -> list[Path]:
    """Generate stable figures from summaries and raw traces."""

    main_rows = [row for row in rows if row.get("campaign_id") == "experiment"]
    normal_rows = [row for row in main_rows if not row.get("adversarial", False)]
    adversarial_rows = [row for row in main_rows if row.get("adversarial", False)]
    trace_rows = adversarial_rows or normal_rows or rows
    paths_and_content = {
        "architecture.svg": ("Architecture", *_architecture_body()),
        "fault-topology.svg": ("Fault topology", *_fault_topology_body()),
        "property-timelines.svg": ("Property timelines", *_timeline_body()),
        "outcome-heatmap.svg": ("Outcome heatmap", *_heatmap_body(summaries)),
        "factorial-interactions.svg": ("Factorial interactions", *_factorial_body(factorial)),
        "latency.svg": (
            "Operation latency by main-campaign condition",
            *_latency_body(_group_summary(normal_rows), _group_summary(adversarial_rows)),
        ),
        "representative-trace.svg": ("Representative trace", *_trace_body(trace_rows)),
        "prediction-observation.svg": ("Prediction versus observation", *_prediction_body(predictions or {}, summaries)),
    }
    paths: list[Path] = []
    converter = shutil.which("rsvg-convert")
    for filename, (title, body, height) in paths_and_content.items():
        path = figures_root / filename
        _svg(path, title, body, height=height)
        paths.append(path)
        if converter:
            for suffix, format_name in ((".pdf", "pdf"), (".png", "png")):
                converted = path.with_suffix(suffix)
                completed = subprocess.run(
                    [converter, "-f", format_name, "-o", str(converted), str(path)],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                if completed.returncode == 0 and converted.is_file():
                    paths.append(converted)
    return paths


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_summary_csv(path: Path, summaries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "campaign_id",
        "adversarial",
        "configuration_id",
        "property",
        "history_count",
        "PASS",
        "VIOLATION",
        "UNAVAILABLE",
        "INDETERMINATE",
        "PRECONDITION_MISS",
        "HARNESS_ERROR",
        "consistency_violation_rate",
        "operation_success_rate",
        "history_completion_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "election_p50_ms",
        "election_p95_ms",
        "election_p99_ms",
        "recovery_p50_ms",
        "recovery_p95_ms",
        "recovery_p99_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            row = {
                "campaign_id": summary["campaign_id"],
                "adversarial": summary["adversarial"],
                "configuration_id": summary["configuration_id"],
                "property": summary["property"],
                "history_count": summary["history_count"],
                **summary["outcomes"],
                "consistency_violation_rate": summary["consistency_violation_rate"],
                "operation_success_rate": summary["operation_success_rate"],
                "history_completion_rate": summary["history_completion_rate"],
                "latency_p50_ms": summary["latency_ms"]["p50"],
                "latency_p95_ms": summary["latency_ms"]["p95"],
                "latency_p99_ms": summary["latency_ms"]["p99"],
                "election_p50_ms": summary["election_ms"]["p50"],
                "election_p95_ms": summary["election_ms"]["p95"],
                "election_p99_ms": summary["election_ms"]["p99"],
                "recovery_p50_ms": summary["recovery_ms"]["p50"],
                "recovery_p95_ms": summary["recovery_ms"]["p95"],
                "recovery_p99_ms": summary["recovery_ms"]["p99"],
            }
            writer.writerow(row)


def analyse(
    *,
    raw_root: Path,
    summary_root: Path,
    figures_root: Path,
    submission_figures_root: Path | None = None,
) -> dict[str, Any]:
    """Rebuild every summary and figure from raw histories."""

    rows = load_rows(raw_root)
    summaries = group_summaries(rows)
    configurations = load_configurations(CONFIG_ROOT / "configurations.json")
    predictions = load_predictions(CONFIG_ROOT / "predictions.json")
    factorial = factorial_analysis(summaries, configurations)
    summary = {
        "schema_version": "summary.v1",
        "status": "DATA" if rows else "NO_DATA",
        "history_count": len(rows),
        "outcome_counts": _counts(rows),
        "overall": _group_summary(rows),
        "campaign_summaries": campaign_summaries(rows),
        "groups": summaries,
        "factorial": factorial,
        "predictions": predictions,
    }
    _write_json(summary_root / "summary.json", summary)
    _write_json(summary_root / "history-results.json", rows)
    _write_json(summary_root / "factorial-contrasts.json", factorial)
    _write_summary_csv(summary_root / "summary.csv", summaries)
    figure_paths = generate_figures(
        figures_root,
        summaries,
        factorial,
        rows,
        predictions=predictions,
    )
    if submission_figures_root is not None:
        submission_figures_root.mkdir(parents=True, exist_ok=True)
        for figure in figure_paths:
            shutil.copy2(figure, submission_figures_root / figure.name)
    return summary
