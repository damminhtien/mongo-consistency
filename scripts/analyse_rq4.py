"""Generate the RQ4 analysis layer without connecting to MongoDB."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

from mongo_consistency.rq4 import PARTITION_SIGNATURES, ROOT, analyse


def _latex_escape(value: Any) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in text)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _number(row: dict[str, str], key: str, default: float | None = None) -> float | None:
    value = row.get(key, "")
    if value in {"", "NA", "NO_DATA", None}:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(row: dict[str, str], key: str) -> int:
    value = _number(row, key, 0)
    return int(value or 0)


def _rate_percent(value: float | None) -> str:
    return "NA" if value is None else f"{value * 100:.1f}\\%"


def _milliseconds(value: float | None) -> str:
    return "NA" if value is None else f"{value:.2f}"


def _scenario_order(value: str) -> tuple[int, str]:
    return ({"normal": 0, "rq1_fault": 1}.get(value, 99), value)


def _config_order(value: str) -> tuple[int, str]:
    try:
        return int(value.removeprefix("C")), value
    except ValueError:
        return 99, value


def _property_order(value: str) -> tuple[int, str]:
    return ({"RYW": 0, "MR": 1, "MW": 2, "WFR": 3}.get(value, 99), value)


def _metric_rows(rows: list[dict[str, str]]) -> str:
    selected = [
        row
        for row in rows
        if row.get("configuration_id") in {"C5", "C6"}
        and row.get("scenario") in {"normal", "rq1_fault"}
    ]
    selected.sort(
        key=lambda row: (
            _scenario_order(row.get("scenario", "")),
            _config_order(row.get("configuration_id", "")),
            _property_order(row.get("property", "")),
        )
    )
    rendered: list[str] = []
    for row in selected:
        counts = "/".join(
            str(_integer(row, outcome))
            for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
        )
        rendered.append(
            f"{_latex_escape(row.get('scenario', ''))} & "
            f"{_latex_escape(row.get('configuration_id', ''))} & "
            f"{_latex_escape(row.get('property', ''))} & {counts} & "
            f"{_rate_percent(_number(row, 'definitive_completion_rate'))} & "
            f"{_milliseconds(_number(row, 'p50_ms'))}/"
            f"{_milliseconds(_number(row, 'p95_ms'))} & "
            f"{_milliseconds(_number(row, 'resolved_p50_ms'))}/"
            f"{_milliseconds(_number(row, 'resolved_p95_ms'))} "
            + r"\\"
        )
    return "\n".join(rendered) or r"\multicolumn{7}{c}{\texttt{NO DATA}} \\"


def _partition_rows(
    rows: list[dict[str, str]],
) -> tuple[str, dict[tuple[str, str], dict[str, str]]]:
    """Render only the balanced C1/C6 RYW and MW signature cells."""

    grouped: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if row.get("scenario") != "partition":
            continue
        key = (row.get("configuration_id", ""), row.get("property", ""))
        if key in PARTITION_SIGNATURES:
            grouped[key] = row
    rendered: list[str] = []
    for config, property_name in PARTITION_SIGNATURES:
        row = grouped.get((config, property_name))
        if row is None:
            rendered.append(
                f"{config} & {property_name} & \\textemdash & \\texttt{{MISSING\\_CELL}} & "
                f"\\texttt{{no RQ2 partition history}} & \\texttt{{NA}} "
                + r"\\"
            )
            continue
        total = _integer(row, "history_count")
        values = "/".join(
            str(_integer(row, outcome))
            for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
        )
        completion = _number(row, "definitive_completion_rate")
        rendered.append(
            f"{config} & {property_name} & {total} & {values} & {_rate_percent(completion)} & "
            f"{_milliseconds(_number(row, 'p95_ms'))} "
            + r"\\"
        )
    return "\n".join(rendered), grouped


def _contrast_rows(
    rows: list[dict[str, str]],
) -> tuple[str, dict[tuple[str, str], dict[str, str]]]:
    selected = [
        row
        for row in rows
        if row.get("left_config") == "C5"
        and row.get("right_config") == "C6"
        and row.get("scenario") in {"normal", "rq1_fault"}
    ]
    selected.sort(
        key=lambda row: (
            _scenario_order(row.get("scenario", "")),
            _property_order(row.get("property", "")),
        )
    )
    rendered: list[str] = []
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for row in selected:
        key = (row.get("scenario", ""), row.get("property", ""))
        by_key[key] = row
        rendered.append(
            f"{_latex_escape(row.get('scenario', ''))} & "
            f"{_latex_escape(row.get('property', ''))} & "
            f"{_latex_escape(row.get('status', ''))} & "
            f"{_milliseconds(_number(row, 'delta_p95_ms'))} & "
            f"{_rate_percent(_number(row, 'delta_definitive_completion_rate'))} \\\\"
        )
    return ("\n".join(rendered) or r"\multicolumn{5}{c}{\texttt{NO DATA}} \\"), by_key


def _partition_signature_values(
    rows: dict[tuple[str, str], dict[str, str]],
    key: tuple[str, str],
) -> tuple[str, str]:
    row = rows.get(key)
    if row is None:
        return "NO_DATA", "NA"
    counts = "/".join(
        str(_integer(row, outcome))
        for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
    )
    return counts, _rate_percent(_number(row, "definitive_completion_rate"))


def _macro(name: str, value: str) -> str:
    return f"\\newcommand{{\\{name}}}{{{value}}}"


def write_report_section(
    summary_root: Path,
    figures_root: Path,
    submission_root: Path,
    summary: dict[str, Any],
) -> Path:
    """Render the RQ4 report section and stage its generated figures."""

    metrics = _read_csv(summary_root / "metrics.csv")
    contrasts = _read_csv(summary_root / "contrasts.csv")
    metric_rows = _metric_rows(metrics)
    partition_rows, partition_counts = _partition_rows(metrics)
    contrast_rows, _ = _contrast_rows(contrasts)

    c1_ryw_counts, c1_ryw_completion = _partition_signature_values(
        partition_counts, ("C1", "RYW")
    )
    c6_ryw_counts, c6_ryw_completion = _partition_signature_values(
        partition_counts, ("C6", "RYW")
    )
    c1_mw_counts, c1_mw_completion = _partition_signature_values(
        partition_counts, ("C1", "MW")
    )
    c6_mw_counts, c6_mw_completion = _partition_signature_values(
        partition_counts, ("C6", "MW")
    )
    macros = {
        "RQFourStatus": _latex_escape(summary.get("status", "NO_DATA")),
        "RQFourHistoryCount": str(summary.get("history_count", 0)),
        "RQFourMetricRowCount": str(summary.get("metric_row_count", 0)),
        "RQFourPartitionCOneRYWCounts": c1_ryw_counts,
        "RQFourPartitionCOneRYWCompletion": c1_ryw_completion,
        "RQFourPartitionCsixRYWCounts": c6_ryw_counts,
        "RQFourPartitionCsixRYWCompletion": c6_ryw_completion,
        "RQFourPartitionCOneMWCounts": c1_mw_counts,
        "RQFourPartitionCOneMWCompletion": c1_mw_completion,
        "RQFourPartitionCsixMWCounts": c6_mw_counts,
        "RQFourPartitionCsixMWCompletion": c6_mw_completion,
        "RQFourSelectedRows": metric_rows,
        "RQFourPartitionRows": partition_rows,
        "RQFourContrastRows": contrast_rows,
    }
    section = "\n".join(_macro(name, value) for name, value in macros.items())
    section += r"""

\subsection{RQ4: Client-visible outcomes under faults}

RQ4 is an offline analysis of the immutable RQ1 and RQ2 histories. It adds no
MongoDB run, workload, topology, or configuration. The analyzer classified
\RQFourHistoryCount\ histories and produced \RQFourMetricRowCount\ configuration,
scenario, and property cells. It asks: when a configuration does not expose a
client-centric consistency violation under a fault, what client-visible outcome
occurs instead: successful completion, waiting or timeout, an ambiguous
outcome, or increased response time?

For each cell, the violation rate is
\(V=\mathrm{VIOLATION}/(\mathrm{PASS}+\mathrm{VIOLATION})\). The completion measure is
the observed definitive completion rate
\(D=(\mathrm{PASS}+\mathrm{VIOLATION})/\mathrm{attempted}\), where attempted includes
PASS, VIOLATION, UNAVAILABLE, and INDETERMINATE. It is not formal CAP
availability. INDETERMINATE is retained as a separate client-visible outcome.
All-attempt latency is the client-observed time from start to end of the
registered critical operation, including valid timeout outcomes. Resolved
latency uses only PASS and VIOLATION histories, so its sample is not silently
mixed with waiting or ambiguous outcomes. Both are reported with p50 and p95;
p99 is not used for these small cells.

\begin{table}[ht]
\centering
\scriptsize
\setlength{\tabcolsep}{3pt}
\caption{RQ4 selected C5/C6 metrics; counts, observed completion, and separate latency samples.}
\label{tab:rq4-selected-metrics}
\begin{tabular}{@{}l l l r r r r@{}}
\toprule
Scenario & Config & Property & P/V/U/I & $D$ & All-attempt p50/p95 (ms) & Resolved p50/p95 (ms) \\
\midrule
\RQFourSelectedRows
\bottomrule
\end{tabular}
\end{table}

\begin{table}[ht]
\centering
\scriptsize
\setlength{\tabcolsep}{4pt}
\caption{RQ4 paired signature outcomes under the RQ2 partition. Each row is one C1/C6 RYW or MW cell.}
\label{tab:rq4-partition-outcomes}
\begin{tabular}{@{}l l r l r r@{}}
\toprule
Config & Property & $n$ & P/V/U/I & $D$ & All-attempt p95 (ms) \\
\midrule
\RQFourPartitionRows
\bottomrule
\end{tabular}
\end{table}

The paired F3 signatures show the same outcome pattern for RYW and MW. C1's
RYW cell has P/V/U/I counts \RQFourPartitionCOneRYWCounts, and observed
definitive completion \RQFourPartitionCOneRYWCompletion; its MW cell has
\RQFourPartitionCOneMWCounts, and \RQFourPartitionCOneMWCompletion. C6's
corresponding RYW cell has \RQFourPartitionCsixRYWCounts\ and
\RQFourPartitionCsixRYWCompletion; its MW cell has
\RQFourPartitionCsixMWCounts, and \RQFourPartitionCsixMWCompletion.
These are paired signature cells, not an aggregate over four properties.

\begin{table}[ht]
\centering
\scriptsize
\setlength{\tabcolsep}{4pt}
\caption{RQ4 C5/C6 one-factor contrasts. Deltas are C6 minus C5.}
\label{tab:rq4-c5-c6}
\begin{tabular}{@{}l l l r r@{}}
\toprule
Scenario & Property & Status & $\Delta p95$ (ms) & $\Delta D$ \\
\midrule
\RQFourContrastRows
\bottomrule
\end{tabular}
\end{table}

The figures retain the available configuration and scenario coverage. They are
descriptive evidence for the tested schedules rather than a scalar ranking of
configurations.

\maybefigure[fig:rq4-outcomes-partition]{submission/figures/rq4_outcomes_partition.pdf}{RQ4 paired C1/C6 RYW and MW signature outcomes under the RQ2 partition.}
\maybefigure[fig:rq4-latency-completion]{submission/figures/rq4_latency_completion.pdf}{RQ4 client-observed all-attempt p95 time against observed definitive completion.}
\maybefigure[fig:rq4-c5-c6]{submission/figures/rq4_c5_c6_contrast.pdf}{RQ4 C5/C6 one-factor contrast under normal and RQ1 fault scenarios.}
"""
    section_path = submission_root / "generated-rq4.tex"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(section.rstrip() + "\n", encoding="utf-8")

    figure_names = (
        "rq4_outcomes_partition.pdf",
        "rq4_latency_completion.pdf",
        "rq4_c5_c6_contrast.pdf",
    )
    figure_destination = submission_root / "figures"
    figure_destination.mkdir(parents=True, exist_ok=True)
    for name in figure_names:
        source = figures_root / name
        destination = figure_destination / name
        if source.is_file():
            shutil.copy2(source, destination)
        elif destination.exists():
            destination.unlink()
    return section_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=ROOT / "results/raw")
    parser.add_argument("--summary-root", type=Path, default=ROOT / "results/summary/rq4")
    parser.add_argument("--figures-root", type=Path, default=ROOT / "figures")
    parser.add_argument("--submission-root", type=Path, default=ROOT / "submission")
    args = parser.parse_args()
    summary = analyse(
        raw_root=args.raw_root,
        summary_root=args.summary_root,
        figures_root=args.figures_root,
    )
    report_section = write_report_section(
        summary_root=args.summary_root,
        figures_root=args.figures_root,
        submission_root=args.submission_root,
        summary=summary,
    )
    print(
        json.dumps(
            {
                "status": summary["status"],
                "history_count": summary["history_count"],
                "report_section": str(report_section),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
