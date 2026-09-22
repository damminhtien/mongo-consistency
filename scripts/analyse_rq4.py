"""Generate the RQ4 analysis layer without connecting to MongoDB."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

from mongo_consistency.rq4 import ROOT, analyse


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
            f"{_milliseconds(_number(row, 'p95_ms'))} "
            + r"\\"
        )
    return "\n".join(rendered) or r"\multicolumn{6}{c}{\texttt{NO DATA}} \\"


def _partition_rows(rows: list[dict[str, str]]) -> tuple[str, dict[str, dict[str, int]]]:
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.get("scenario") != "partition":
            continue
        config = row.get("configuration_id", "")
        counts = grouped.setdefault(
            config,
            {
                outcome: 0
                for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
            },
        )
        for outcome in counts:
            counts[outcome] += _integer(row, outcome)
    rendered: list[str] = []
    for config in ("C1", "C3", "C4", "C5", "C6"):
        counts = grouped.get(config)
        if counts is None:
            rendered.append(
                f"{config} & \\textemdash & \\texttt{{MISSING\\_CELL}} & "
                f"\\texttt{{no RQ2 partition histories}} "
                + r"\\"
            )
            continue
        total = sum(counts.values())
        completion = (counts["PASS"] + counts["VIOLATION"]) / total if total else None
        values = "/".join(
            str(counts[outcome])
            for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
        )
        rendered.append(
            f"{config} & {total} & {values} & {_rate_percent(completion)} "
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

    c1_partition = partition_counts.get("C1", {})
    c6_partition = partition_counts.get("C6", {})
    c1_total = sum(c1_partition.values())
    c6_total = sum(c6_partition.values())
    c1_completion = (
        (c1_partition.get("PASS", 0) + c1_partition.get("VIOLATION", 0)) / c1_total
        if c1_total
        else None
    )
    c6_completion = (
        (c6_partition.get("PASS", 0) + c6_partition.get("VIOLATION", 0)) / c6_total
        if c6_total
        else None
    )
    macros = {
        "RQFourStatus": _latex_escape(summary.get("status", "NO_DATA")),
        "RQFourHistoryCount": str(summary.get("history_count", 0)),
        "RQFourMetricRowCount": str(summary.get("metric_row_count", 0)),
        "RQFourPartitionCOneCounts": (
            "/".join(
                str(c1_partition.get(outcome, 0))
                for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
            )
            if c1_partition
            else "NO_DATA"
        ),
        "RQFourPartitionCOneCompletion": _rate_percent(c1_completion),
        "RQFourPartitionCsixCounts": (
            "/".join(
                str(c6_partition.get(outcome, 0))
                for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
            )
            if c6_partition
            else "NO_DATA"
        ),
        "RQFourPartitionCsixCompletion": _rate_percent(c6_completion),
        "RQFourPartitionCfiveStatus": "MISSING\\_CELL" if "C5" not in partition_counts else "DATA",
        "RQFourSelectedRows": metric_rows,
        "RQFourPartitionRows": partition_rows,
        "RQFourContrastRows": contrast_rows,
    }
    section = "\n".join(_macro(name, value) for name, value in macros.items())
    section += r"""

\subsection{RQ4: Consistency, completion, and latency costs}

RQ4 is an offline analysis of the immutable RQ1 and RQ2 histories. It adds no
MongoDB run, workload, topology, or configuration. The analyzer classified
\RQFourHistoryCount\ histories and produced \RQFourMetricRowCount\ configuration,
scenario, and property cells. It asks how much observed client cost accompanies
avoiding a completed consistency violation under the recorded schedules.

For each cell, the violation rate is
\(V=\mathrm{VIOLATION}/(\mathrm{PASS}+\mathrm{VIOLATION})\). The completion measure is
the observed definitive completion rate
\(D=(\mathrm{PASS}+\mathrm{VIOLATION})/\mathrm{attempted}\), where attempted includes
PASS, VIOLATION, UNAVAILABLE, and INDETERMINATE. It is not formal CAP
availability. INDETERMINATE is retained as a separate client-visible outcome.
Latency is measured for the registered critical operation of each property and
reported with p50 and p95; p99 is not used for these small cells.

\begin{table}[ht]
\centering
\scriptsize
\setlength{\tabcolsep}{3pt}
\caption{RQ4 selected C5/C6 metrics; counts and observed completion.}
\label{tab:rq4-selected-metrics}
\begin{tabular}{@{}l l l r r r@{}}
\toprule
Scenario & Config & Property & P/V/U/I & $D$ & p50/p95 (ms) \\
\midrule
\RQFourSelectedRows
\bottomrule
\end{tabular}
\end{table}

\begin{table}[ht]
\centering
\scriptsize
\setlength{\tabcolsep}{4pt}
\caption{RQ4 outcome composition under the RQ2 partition. Rows aggregate the four property cells.}
\label{tab:rq4-partition-outcomes}
\begin{tabular}{@{}l r l r@{}}
\toprule
Config & $n$ & P/V/U/I & $D$ \\
\midrule
\RQFourPartitionRows
\bottomrule
\end{tabular}
\end{table}

The partition contrast is visible in the outcome composition. C1's P/V/U/I
counts are \RQFourPartitionCOneCounts\ and its observed definitive completion
is \RQFourPartitionCOneCompletion\  C6's P/V/U/I counts are
\RQFourPartitionCsixCounts\ and its observed definitive completion is
\RQFourPartitionCsixCompletion\  The C5 partition cell is
\RQFourPartitionCfiveStatus: C5 was not included in
the RQ2 partition campaign, so no C5/C6 partition latency or completion
contrast is inferred.

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

The figures retain the full configuration and scenario coverage. They are
descriptive evidence for the tested schedules rather than a scalar ranking of
configurations.

\maybefigure[fig:rq4-outcomes-partition]{submission/figures/rq4_outcomes_partition.pdf}{RQ4 outcome composition under the RQ2 partition.}
\maybefigure[fig:rq4-latency-completion]{submission/figures/rq4_latency_completion.pdf}{RQ4 critical-operation p95 latency against observed definitive completion.}
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
