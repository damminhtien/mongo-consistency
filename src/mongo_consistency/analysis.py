"""Deterministic offline summaries and figures from raw histories."""

from __future__ import annotations

import csv
import html
import json
import math
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .checkers import check_history
from .config import CONFIG_ROOT, load_configurations, load_predictions
from .history import read_history
from .models import History, Outcome


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


def _history_row(path: Path, raw_root: Path) -> dict[str, Any]:
    relative = path.relative_to(raw_root).as_posix()
    try:
        history = read_history(path)
    except Exception as error:  # Malformed input is visible as a harness row.
        return {
            "path": relative,
            "outcome": Outcome.HARNESS_ERROR.value,
            "reason": f"history could not be loaded: {error}",
            "configuration_id": None,
            "property": None,
            "campaign_id": None,
            "adversarial": None,
            "operation_metrics": {"attempted": 0, "successful": 0, "success_rate": None, "latency_ms": {}},
        }
    result = check_history(history)
    manifest = history.manifest
    operations = _operation_metrics(history)
    return {
        "path": relative,
        "history_hash": history.history_hash,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "details": result.details,
        "configuration_id": manifest.get("configuration_id"),
        "property": manifest.get("property"),
        "campaign_id": manifest.get("campaign_id"),
        "adversarial": manifest.get("adversarial", False),
        "seed": manifest.get("seed"),
        "operation_metrics": operations,
    }


def load_rows(raw_root: Path) -> list[dict[str, Any]]:
    """Load all canonical trial JSON files below a raw-results root."""

    if not raw_root.exists():
        return []
    paths = sorted(
        path
        for path in raw_root.rglob("*.json")
        if path.name != "campaign-manifest.json"
    )
    return [_history_row(path, raw_root) for path in paths]


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
        "latency_ms": {
            "p50": quantile(latency_values, 0.50),
            "p95": quantile(latency_values, 0.95),
            "p99": quantile(latency_values, 0.99),
        },
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


def _mean(values: Iterable[float]) -> float | None:
    values_list = list(values)
    return sum(values_list) / len(values_list) if values_list else None


def _factorial_contrast(
    cells: dict[str, float],
    configurations: dict[str, dict[str, Any]],
    factors: tuple[str, ...],
) -> float | None:
    signed: list[tuple[int, float]] = []
    for configuration_id, value in cells.items():
        configuration = configurations.get(configuration_id)
        if configuration is None:
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
    """Compute RC, WC, CS main effects and interactions for main adversarial cells."""

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
    output: dict[str, Any] = {"status": "NO_DATA", "properties": {}}
    for property_name, property_summaries in sorted(by_property.items()):
        cells = {
            str(summary["configuration_id"]): summary["consistency_violation_rate"]
            for summary in property_summaries
            if summary["consistency_violation_rate"] is not None
        }
        property_output = {
            "cell_values": cells,
            "main_effects": {
                name: _factorial_contrast(cells, configurations, (factor,))
                for name, factor in factors.items()
            },
            "interactions": {
                name: _factorial_contrast(cells, configurations, factor_tuple)
                for name, factor_tuple in interactions.items()
            },
            "delta_formulas": {
                "Delta_CS": "Y(rc,wc,on) - Y(rc,wc,off)",
                "Delta_RC": "Y(majority,wc,cs) - Y(local,wc,cs)",
                "Delta_WC": "Y(rc,majority,cs) - Y(rc,w:1,cs)",
            },
        }
        if cells:
            output["status"] = "DATA"
        output["properties"][property_name] = property_output
    return output


def _svg(path: Path, title: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        f'<text x="40" y="{80 + index * 28}" font-family="sans-serif" font-size="16">{html.escape(line)}</text>'
        for index, line in enumerate(lines)
    )
    height = max(140, 110 + len(lines) * 28)
    content = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" '
        f'height="{height}" viewBox="0 0 1000 {height}">'
        '<rect width="100%" height="100%" fill="white"/>'
        f'<text x="40" y="42" font-family="sans-serif" font-size="22" font-weight="bold">{html.escape(title)}</text>'
        f"{text}</svg>\n"
    )
    path.write_text(content, encoding="utf-8")


def generate_figures(
    figures_root: Path,
    summaries: list[dict[str, Any]],
    factorial: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[Path]:
    """Generate stable SVG figures, including explicit no-data labels."""

    has_data = bool(rows)
    marker = "Recorded histories available" if has_data else "NO_DATA: run make pilot or make experiment"
    paths_and_content = {
        "architecture.svg": ("Architecture", ["runner -> client_net -> MongoDB members", "replica_net carries member replication and election traffic", "fault sidecars control only replica-network traffic"]),
        "fault-topology.svg": ("Fault topology", ["normal: all three members connected", "RYW and MR: one secondary replication path isolated", "MW and WFR: old primary isolated, remaining members elect"]),
        "property-timelines.svg": ("Property timelines", ["RYW: W(x) then R(x)", "MR: R1(x) then R2(x)", "MW: W1(x) then W2(x) then one observer snapshot", "WFR: R1(x) then dependent W2(x) then one observer snapshot"]),
        "outcome-heatmap.svg": ("Outcome heatmap", [marker, f"summary groups: {len(summaries)}"]),
        "factorial-interactions.svg": ("Factorial interactions", [f"status: {factorial.get('status', 'NO_DATA')}", "factors: read concern, write concern, causal session"]),
        "latency.svg": ("Latency", [marker, "p50, p95, and p99 are computed from operation timings"]),
        "representative-trace.svg": ("Representative trace", [marker, "requested member and actual server address are retained per operation"]),
        "prediction-observation.svg": ("Prediction versus observation", [marker, "predictions are read from configs/predictions.json"]),
    }
    paths: list[Path] = []
    for filename, (title, lines) in paths_and_content.items():
        path = figures_root / filename
        _svg(path, title, lines)
        paths.append(path)
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
        "HARNESS_ERROR",
        "UNSUPPORTED",
        "consistency_violation_rate",
        "operation_success_rate",
        "history_completion_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
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
        "schema_version": "analysis.v1",
        "status": "DATA" if rows else "NO_DATA",
        "history_count": len(rows),
        "outcome_counts": _counts(rows),
        "groups": summaries,
        "factorial": factorial,
        "predictions": predictions,
    }
    _write_json(summary_root / "summary.json", summary)
    _write_json(summary_root / "history-results.json", rows)
    _write_json(summary_root / "factorial-contrasts.json", factorial)
    _write_summary_csv(summary_root / "summary.csv", summaries)
    figure_paths = generate_figures(figures_root, summaries, factorial, rows)
    if submission_figures_root is not None:
        submission_figures_root.mkdir(parents=True, exist_ok=True)
        for figure in figure_paths:
            shutil.copy2(figure, submission_figures_root / figure.name)
    return summary
