"""Rebuild client-visible completion and latency data from saved histories."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from mongo_consistency.rq4 import PARTITION_SIGNATURES, analyse

ROOT = Path(__file__).resolve().parents[1]


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


def _value(value: Any) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    return "".join(replacements.get(character, character) for character in text)


def _rate_percent(value: float | None) -> str:
    return "NA" if value is None else f"{value * 100:.1f}\\%"


def _milliseconds(value: float | None) -> str:
    return "NA" if value is None else f"{value:.2f}"


def _scenario_order(value: str) -> tuple[int, str]:
    return ({"normal": 0, "rq1_fault": 1}.get(value, 99), value)


def _property_order(value: str) -> tuple[int, str]:
    return ({"RYW": 0, "MR": 1, "MW": 2, "WFR": 3}.get(value, 99), value)


def _metric_rows(rows: list[dict[str, str]]) -> str:
    selected = {
        (row.get("scenario"), row.get("configuration_id"), row.get("property")): row
        for row in rows
        if row.get("configuration_id") in {"C5", "C6"}
        and row.get("scenario") in {"normal", "rq1_fault"}
    }
    rendered = []
    for configuration in ("C5", "C6"):
        for property_name in ("RYW", "MR", "MW", "WFR"):
            normal = selected.get(("normal", configuration, property_name))
            adversarial = selected.get(("rq1_fault", configuration, property_name))
            if normal is None or adversarial is None:
                return r"\multicolumn{7}{c}{\texttt{NO DATA}} \\"
            counts = "/".join(
                str(_integer(adversarial, outcome))
                for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
            )
            rendered.append(
                f"{configuration} & {property_name} & "
                f"{_milliseconds(_number(normal, 'p95_ms'))} & {counts} & "
                f"{_rate_percent(_number(adversarial, 'definitive_completion_rate'))} & "
                f"{_milliseconds(_number(adversarial, 'p95_ms'))} & "
                f"{_milliseconds(_number(adversarial, 'resolved_p95_ms'))} "
                + r"\\"
            )
    return "\n".join(rendered) or r"\multicolumn{7}{c}{\texttt{NO DATA}} \\"


def _partition_rows(
    rows: list[dict[str, str]],
) -> tuple[str, dict[tuple[str, str], dict[str, str]]]:
    grouped: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if row.get("scenario") != "partition":
            continue
        key = (row.get("configuration_id", ""), row.get("property", ""))
        if key in PARTITION_SIGNATURES:
            grouped[key] = row
    rendered = []
    for config, property_name in PARTITION_SIGNATURES:
        row = grouped.get((config, property_name))
        if row is None:
            rendered.append(
                f"{config} & {property_name} & -- & \\texttt{{MISSING_CELL}} & "
                f"\\texttt{{no partition history}} & NA " + r"\\"
            )
            continue
        total = _integer(row, "history_count")
        values = "/".join(
            str(_integer(row, outcome))
            for outcome in ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE")
        )
        rendered.append(
            f"{config} & {property_name} & {total} & {values} & "
            f"{_rate_percent(_number(row, 'definitive_completion_rate'))} & "
            f"{_milliseconds(_number(row, 'p95_ms'))} " + r"\\"
        )
    return "\n".join(rendered), grouped


def _contrast_rows(rows: list[dict[str, str]]) -> str:
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
    rendered = [
        f"{_value(row.get('scenario', ''))} & {_value(row.get('property', ''))} & "
        f"{_value(row.get('status', ''))} & {_milliseconds(_number(row, 'delta_p95_ms'))} & "
        f"{_rate_percent(_number(row, 'delta_definitive_completion_rate'))} " + r"\\"
        for row in selected
    ]
    return "\n".join(rendered) or r"\multicolumn{5}{c}{\texttt{NO DATA}} \\"


def _partition_signature_values(
    rows: dict[tuple[str, str], dict[str, str]], key: tuple[str, str]
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


def write_data_macros(summary_root: Path, output: Path, summary: dict[str, Any]) -> Path:
    """Write numeric and table-row values; prose remains in checked-in TeX."""

    metrics = _read_csv(summary_root / "metrics.csv")
    contrasts = _read_csv(summary_root / "contrasts.csv")
    partition_rows, partition_counts = _partition_rows(metrics)
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
    values = {
        "RQFourStatus": _value(summary.get("status", "NO_DATA")),
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
        "RQFourSelectedRows": _metric_rows(metrics),
        "RQFourPartitionRows": partition_rows,
        "RQFourContrastRows": _contrast_rows(contrasts),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(_macro(name, value) for name, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=ROOT / "results/raw")
    parser.add_argument("--summary-root", type=Path, default=ROOT / "results/summary/rq4")
    parser.add_argument("--figures-root", type=Path, default=ROOT / "figures")
    parser.add_argument(
        "--data-output",
        type=Path,
        default=ROOT / "submission/generated-rq4-data.tex",
    )
    args = parser.parse_args()
    summary = analyse(
        raw_root=args.raw_root,
        summary_root=args.summary_root,
        figures_root=args.figures_root,
    )
    write_data_macros(args.summary_root, args.data_output, summary)
    print(
        json.dumps(
            {
                "status": summary["status"],
                "history_count": summary["history_count"],
                "metric_row_count": summary["metric_row_count"],
                "data_output": args.data_output.as_posix(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
