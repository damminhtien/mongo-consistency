"""Print-sized vector figures for the report; never writes report prose."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import scienceplots  # noqa: F401  # Registers the SciencePlots styles.
from matplotlib.patches import FancyArrowPatch, Rectangle

INK = "#20252b"
MUTED = "#59636e"
BLUE = "#245b87"
ORANGE = "#b76426"
GREEN = "#28734b"
RED = "#a13f3b"
GRAY = "#e6e8e9"
PALE_BLUE = "#dce8f0"


def _style() -> None:
    plt.style.use(["science", "no-latex"])
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["DejaVu Serif"],
            "font.size": 9.5,
            "axes.labelsize": 9.5,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.fontsize": 8.5,
            "pdf.fonttype": 42,
            "svg.fonttype": "none",
            "savefig.facecolor": "white",
        }
    )


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", pad_inches=0.10)
    plt.close(fig)
    return path


def _box(ax: Any, x: float, y: float, label: str, *, width: float = 1.55, color: str = PALE_BLUE) -> None:
    ax.add_patch(Rectangle((x, y - 0.26), width, 0.52, facecolor=color, edgecolor=INK, linewidth=0.85))
    ax.text(x + width / 2, y, label, ha="center", va="center", fontsize=9)


def _arrow(ax: Any, start: tuple[float, float], end: tuple[float, float], *, color: str = INK) -> None:
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=10, linewidth=0.9, color=color))


def _diagram_axis(figsize: tuple[float, float], xlim: tuple[float, float], ylim: tuple[float, float]) -> tuple[Any, Any]:
    fig, ax = plt.subplots(figsize=figsize)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.axis("off")
    return fig, ax


def architecture(path: Path) -> Path:
    fig, ax = _diagram_axis((6.8, 3.15), (0, 8.6), (-0.15, 3.7))
    _box(ax, 0.2, 1.8, "Client / runner", width=1.75, color="#e0ede5")
    for y, name in ((3.05, "mongo1"), (1.8, "mongo2"), (0.55, "mongo3")):
        _box(ax, 3.1, y, name)
        _arrow(ax, (1.96, 1.8), (3.07, y))
    _box(ax, 6.65, 1.8, "Fault controller", width=1.72, color="#f5e4df")
    for y in (3.05, 1.8, 0.55):
        _arrow(ax, (6.62, 1.8), (4.68, y), color=ORANGE)
    ax.text(2.5, 3.50, "client network", ha="center", fontsize=8.5, color=BLUE)
    ax.text(5.57, 3.50, "replication-path control", ha="center", fontsize=8.5, color=ORANGE)
    ax.text(4.3, -0.02, "One elected primary and two secondaries; roles may change after election.", ha="center", fontsize=8)
    return _save(fig, path)


def fault_topology(path: Path) -> Path:
    fig, ax = _diagram_axis((6.8, 2.85), (0, 8.4), (0, 3.55))
    for y, label in ((2.75, "Healthy replica path"), (1.75, "Lagging secondary"), (0.75, "Election after isolation")):
        ax.text(0.05, y, label, va="center", fontsize=8.5)
    for y, left, right, color in ((2.75, "Primary", "Secondary", BLUE), (1.75, "Primary", "Lagging", RED), (0.75, "Former primary", "New primary", ORANGE)):
        _box(ax, 3.25, y, left, width=1.65, color="#e0ede5" if y == 2.75 else PALE_BLUE)
        _box(ax, 6.55, y, right, width=1.65, color="#f5e4df" if y < 2.75 else PALE_BLUE)
        _arrow(ax, (4.94, y), (6.50, y), color=color)
    return _save(fig, path)


def property_timelines(path: Path) -> Path:
    schedules = (
        ("RYW", ["write v1", "read x"], "Read may be routed to a lagging member"),
        ("MR", ["read v1", "read x"], "Second read may return an older version"),
        ("MW", ["W1", "election", "W2"], "Check W1 in W2's atomic pre-image"),
        ("WFR", ["read v1", "election", "W2"], "Check v1 against W2's atomic pre-image"),
    )
    fig, ax = _diagram_axis((6.8, 3.8), (0, 7.65), (-0.25, 4.1))
    for index, (property_name, steps, explanation) in enumerate(schedules):
        y = 3.55 - index
        ax.text(0.05, y, property_name, va="center", fontweight="bold")
        ax.text(1.02, y - 0.29, explanation, fontsize=7.8, color=MUTED)
        for step_index, step in enumerate(steps):
            x = 1.10 + step_index * 2.05
            _box(ax, x, y + 0.13, step, width=1.46, color="#f5e4df" if step == "election" else PALE_BLUE)
            if step_index:
                _arrow(ax, (x - 0.55, y + 0.13), (x - 0.06, y + 0.13))
    return _save(fig, path)


def _summary_for(summaries: list[dict[str, Any]], config: str, property_name: str) -> dict[str, Any] | None:
    return next((row for row in summaries if row.get("campaign_id") == "experiment" and row.get("adversarial") and row.get("configuration_id") == config and row.get("property") == property_name), None)


def prediction_observation(path: Path, predictions: dict[str, dict[str, Any]], summaries: list[dict[str, Any]]) -> Path:
    fig, ax = plt.subplots(figsize=(6.8, 4.15))
    properties = ("RYW", "MR", "MW", "WFR")
    configs = tuple(f"C{i}" for i in range(1, 9))
    for row_index, config in enumerate(configs):
        targets = set(predictions.get(config, {}).get("guarantee_targets", ()))
        for col_index, property_name in enumerate(properties):
            row = _summary_for(summaries, config, property_name)
            counts = row.get("outcomes", {}) if row else {}
            passed = int(counts.get("PASS", 0))
            violated = int(counts.get("VIOLATION", 0))
            unresolved = sum(int(counts.get(key, 0)) for key in ("UNAVAILABLE", "INDETERMINATE", "PRECONDITION_MISS"))
            if not row:
                color, label = GRAY, "no data"
            elif passed + violated:
                color = "#f1d1cf" if violated else "#d9eadf"
                label = f"{violated}/{passed + violated} V"
            else:
                color, label = "#e7e8ec", f"{unresolved} unresolved"
            border = INK if property_name in targets else "#c6ccd0"
            linewidth = 1.65 if property_name in targets else 0.65
            ax.add_patch(Rectangle((col_index - 0.47, row_index - 0.43), 0.94, 0.86, facecolor=color, edgecolor=border, linewidth=linewidth))
            ax.text(col_index, row_index, label, ha="center", va="center", fontsize=8)
    ax.set_xlim(-0.5, 3.5)
    ax.set_ylim(7.5, -0.5)
    ax.set_xticks(range(4), properties)
    ax.set_yticks(range(8), configs)
    ax.xaxis.tick_top()
    ax.minorticks_off()
    ax.tick_params(which="both", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.text(0.5, 0.025, "Cell: violations / decidable histories; dark border: guarantee target.", ha="center", fontsize=8.5)
    return _save(fig, path)


def outcome_heatmap(path: Path, summaries: list[dict[str, Any]]) -> Path:
    return prediction_observation(path, {}, summaries)


def factorial_interactions(path: Path, factorial: dict[str, Any]) -> Path:
    labels = (("RC", "read_concern"), ("WC", "write_concern"), ("CS", "causal_session"), ("RC×CS", "read_concern_x_causal_session"), ("WC×CS", "write_concern_x_causal_session"), ("RC×WC", "read_concern_x_write_concern"), ("3-way", "read_concern_x_write_concern_x_causal_session"))
    fig, ax = plt.subplots(figsize=(6.8, 3.5))
    property_names = ("RYW", "MR", "MW", "WFR")
    for row_index, property_name in enumerate(property_names):
        values = factorial.get("properties", {}).get(property_name, {})
        effects = values.get("main_effects", {}) | values.get("interactions", {})
        for col_index, (_label, key) in enumerate(labels):
            value = effects.get(key)
            color = GRAY if value is None else ("#d9eadf" if float(value) >= 0 else "#f1d1cf")
            ax.add_patch(Rectangle((col_index - 0.48, row_index - 0.43), 0.96, 0.86, facecolor=color, edgecolor="white"))
            ax.text(col_index, row_index, "NA" if value is None else f"{float(value):+.3f}", ha="center", va="center", fontsize=8)
    ax.set_xlim(-0.5, 6.5)
    ax.set_ylim(3.5, -0.5)
    ax.set_xticks(range(7), [label for label, _ in labels])
    ax.set_yticks(range(4), property_names)
    ax.xaxis.tick_top()
    ax.minorticks_off()
    ax.tick_params(which="both", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return _save(fig, path)


def latency(path: Path, normal: dict[str, Any], adversarial: dict[str, Any]) -> Path:
    fig, ax = plt.subplots(figsize=(6.8, 3.35))
    labels = ("p50", "p95", "p99")
    for offset, (group, color, hatch) in enumerate(((normal, BLUE, ""), (adversarial, ORANGE, "///"))):
        values = [group.get("latency_ms", {}).get(key) for key in labels]
        bars = ax.bar([i + (offset - 0.5) * 0.36 for i in range(3)], [float(value or 0) for value in values], width=0.34, label="Normal control" if offset == 0 else "Adversarial", facecolor=color, edgecolor=INK, linewidth=0.55, hatch=hatch)
        for bar, value in zip(bars, values, strict=True):
            if value is not None and float(value) > 0:
                ax.annotate(f"{float(value):.1f}", (bar.get_x() + bar.get_width() / 2, float(value)), xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8)
    ax.set_xticks(range(3), labels)
    if any(float(value or 0) > 0 for group in (normal, adversarial) for value in group.get("latency_ms", {}).values()):
        ax.set_yscale("log")
        ax.set_ylim(top=max(float(value or 0) for group in (normal, adversarial) for value in group.get("latency_ms", {}).values()) * 1.7)
        ax.set_ylabel("Operation latency (ms, log scale)")
    else:
        ax.set_ylabel("Operation latency (ms)")
        ax.text(0.5, 0.5, "No operation timings", ha="center", transform=ax.transAxes)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, ncol=2, loc="upper left")
    return _save(fig, path)


def representative_trace(path: Path, rows: list[dict[str, Any]]) -> Path:
    row = next((item for item in rows if item.get("configuration_id") == "C6" and item.get("property") == "RYW" and item.get("outcome") == "UNAVAILABLE" and item.get("trace")), None)
    if row is None:
        row = next((item for item in rows if item.get("trace")), None)
    fig, ax = _diagram_axis((6.8, 3.05), (0, 7.4), (-0.2, 3.2))
    if row is None:
        ax.text(3.7, 1.2, "No recorded operation trace", ha="center")
        return _save(fig, path)
    operations = {operation.get("operation_id"): operation for operation in row["trace"]}
    write = operations.get("write", {})
    read = operations.get("read", {})
    steps = (
        ("Partition", "Replica path isolated", PALE_BLUE),
        ("Write", f"majority: {write.get('status', 'NA')}", "#e0ede5"),
        ("Read", f"causal majority: {read.get('status', 'NA')}", "#f5e4df"),
    )
    for index, (label, detail, color) in enumerate(steps):
        x = 0.13 + index * 2.47
        _box(ax, x, 2.57, label, width=1.90, color=color)
        ax.text(x + 0.95, 1.93, detail, ha="center", fontsize=8)
        if index:
            _arrow(ax, (x - 0.54, 2.57), (x - 0.08, 2.57))
    write_route = f"write route: {write.get('actual_server_address') or 'not recorded'} ({write.get('actual_role') or 'unknown'})"
    read_route = f"read route: {read.get('actual_server_address') or 'not recorded'} ({read.get('actual_role') or 'unknown'})"
    after = read.get("after_cluster_time") or {}
    clock = f"afterClusterTime: ({after.get('seconds', 'NA')}, {after.get('increment', 'NA')})"
    duration = read.get("duration_ms")
    duration_text = f"{float(duration) / 1000:.2f} s" if isinstance(duration, (int, float)) else "unrecorded duration"
    outcome = f"{read.get('error_code') or 'no error code'} after {duration_text}; no read value returned"
    for y, detail in ((1.35, write_route), (1.06, read_route), (0.77, clock), (0.48, outcome)):
        ax.text(0.28, y, detail, ha="left", fontsize=8.3)
    return _save(fig, path)


def generate_main_figures(
    root: Path,
    summaries: list[dict[str, Any]],
    factorial: dict[str, Any],
    rows: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
    normal: dict[str, Any],
    adversarial: dict[str, Any],
) -> list[Path]:
    _style()
    return [
        architecture(root / "architecture.pdf"),
        fault_topology(root / "fault-topology.pdf"),
        property_timelines(root / "property-timelines.pdf"),
        prediction_observation(root / "prediction-observation.pdf", predictions, summaries),
        outcome_heatmap(root / "outcome-heatmap.pdf", summaries),
        factorial_interactions(root / "factorial-interactions.pdf", factorial),
        latency(root / "latency.pdf", normal, adversarial),
        representative_trace(root / "representative-trace.pdf", rows),
    ]


def rq4_outcomes_partition(path: Path, rows: list[dict[str, Any]]) -> Path:
    from .rq4 import PARTITION_SIGNATURES, _partition_counts

    signatures = PARTITION_SIGNATURES
    counts = _partition_counts(rows)
    outcomes = (("PASS", GREEN), ("VIOLATION", RED), ("UNAVAILABLE", ORANGE), ("INDETERMINATE", BLUE))
    fig, ax = plt.subplots(figsize=(6.8, 3.35))
    for index, signature in enumerate(signatures):
        left = 0
        for outcome, color in outcomes:
            count = counts[signature][outcome]
            if count:
                ax.barh(index, count, left=left, height=0.65, color=color, edgecolor=INK, linewidth=0.45, label=outcome if index == 0 else None)
                ax.text(left + count / 2, index, str(count), ha="center", va="center", color="white" if count > 1 else INK, fontsize=8.5)
            left += count
        if not any(counts[signature].values()):
            ax.text(0.15, index, "not recorded", va="center", color=MUTED)
    ax.set_yticks(range(4), [f"{config} {property_name}" for config, property_name in signatures])
    ax.invert_yaxis()
    ax.set_xlabel("Histories")
    ax.set_xlim(0, max(1, max((sum(int(row.get(outcome, 0)) for outcome, _ in outcomes) for row in rows if row.get("scenario") == "partition"), default=1)) + 1)
    handles = [Rectangle((0, 0), 1, 1, color=color) for _, color in outcomes]
    ax.legend(handles, [name.title() for name, _ in outcomes], ncol=2, frameon=False, loc="upper right")
    ax.grid(axis="x", alpha=0.25)
    return _save(fig, path)


def rq4_latency_completion(path: Path, rows: list[dict[str, Any]]) -> Path:
    signatures = (("C1", "RYW"), ("C6", "RYW"), ("C1", "MW"), ("C6", "MW"))
    selected = {
        (row.get("configuration_id"), row.get("property")): row
        for row in rows
        if row.get("scenario") == "partition"
        and (row.get("configuration_id"), row.get("property")) in signatures
    }
    fig, (completion_ax, latency_ax) = plt.subplots(2, 1, figsize=(6.8, 4.45), sharex=True, gridspec_kw={"height_ratios": [1, 1]})
    for index, signature in enumerate(signatures):
        row = selected.get(signature)
        if row is None:
            completion_ax.text(index, 0.5, "not recorded", ha="center", va="center", rotation=90, fontsize=7.5)
            latency_ax.text(index, 0.5, "not recorded", ha="center", va="center", rotation=90, fontsize=7.5)
            continue
        completion = row.get("definitive_completion_rate")
        if completion is not None:
            completion_ax.bar(index, float(completion), color=RED if int(row.get("VIOLATION", 0)) else BLUE, edgecolor=INK, linewidth=0.6, width=0.65)
            completion_ax.text(index, float(completion) + 0.04, f"{float(completion):.0%}", ha="center", fontsize=8.5)
        latency_ms = row.get("p95_ms")
        if latency_ms is None:
            latency_ax.text(index, 0.5, "not issued", ha="center", va="center", fontsize=8)
        else:
            latency_ax.bar(index, float(latency_ms), color=ORANGE, edgecolor=INK, linewidth=0.6, width=0.65)
            latency_ax.text(index, float(latency_ms) + 0.12, f"{float(latency_ms):.2f}", ha="center", fontsize=8.5)
    completion_ax.set_ylim(0, 1.17)
    completion_ax.set_ylabel("Definitive completion")
    latency_ax.set_ylabel("p95 latency (ms)")
    latency_ax.set_ylim(0, max((float(row["p95_ms"]) for row in selected.values() if row.get("p95_ms") is not None), default=1) * 1.22)
    latency_ax.set_xticks(range(4), [f"{config} {property_name}" for config, property_name in signatures])
    for ax in (completion_ax, latency_ax):
        ax.grid(axis="y", alpha=0.25)
        ax.set_axisbelow(True)
    fig.subplots_adjust(hspace=0.24)
    return _save(fig, path)


def rq4_c5_c6_contrast(path: Path, contrasts: list[dict[str, Any]]) -> Path:
    selected = [row for row in contrasts if row.get("left_config") == "C5" and row.get("right_config") == "C6" and row.get("scenario") in {"normal", "rq1_fault"}]
    fig, ax = plt.subplots(figsize=(6.8, max(3.4, 0.33 * len(selected) + 1.25)))
    for index, row in enumerate(selected):
        left = row.get("left_p95_ms")
        right = row.get("right_p95_ms")
        if left is not None and right is not None:
            ax.plot((float(left), float(right)), (index, index), color="#9ba3a9", linewidth=1.1, zorder=1)
        for offset, (field, color, label) in enumerate((("left_p95_ms", BLUE, "C5"), ("right_p95_ms", ORANGE, "C6"))):
            value = row.get(field)
            if value is not None:
                ax.scatter(float(value), index + (offset - 0.5) * 0.12, color=color, edgecolor=INK, linewidth=0.55, s=42, label=label if index == 0 else None, zorder=2)
    ax.set_yticks(range(len(selected)), [f"{row['scenario']} / {row['property']}" for row in selected])
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("Critical-operation p95 latency (ms, log scale)")
    ax.grid(axis="x", alpha=0.25)
    if selected:
        ax.legend(frameon=False, ncol=2)
    else:
        ax.text(0.5, 0.5, "No matched C5/C6 cells", ha="center", transform=ax.transAxes)
    return _save(fig, path)


def generate_rq4_figures(root: Path, rows: list[dict[str, Any]], contrasts: list[dict[str, Any]]) -> list[Path]:
    _style()
    return [
        rq4_outcomes_partition(root / "rq4_outcomes_partition.pdf", rows),
        rq4_latency_completion(root / "rq4_latency_completion.pdf", rows),
        rq4_c5_c6_contrast(root / "rq4_c5_c6_contrast.pdf", contrasts),
    ]
