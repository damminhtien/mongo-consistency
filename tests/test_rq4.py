from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.history import write_history
from mongo_consistency.models import History, OperationRecord
from scripts.analyse_rq4 import _metric_rows
from mongo_consistency.rq4 import (
    PARTITION_SIGNATURES,
    _partition_counts,
    analyse,
    contrast_rows,
    fault_delta_rows,
    load_records,
    metric_rows,
)


def _ryw_history(
    *,
    campaign_id: str,
    configuration_id: str,
    adversarial: bool,
    topology_condition: str | None = None,
) -> History:
    manifest = {
        "schema_version": "manifest.v1",
        "trial_id": f"{campaign_id}-C1-ryw",
        "campaign_id": campaign_id,
        "configuration_id": configuration_id,
        "property": "RYW",
        "adversarial": adversarial,
        "topology_condition": topology_condition,
        "property_steps": {"write": "write", "read": "read"},
    }
    return History(
        manifest=manifest,
        operations=[
            OperationRecord(
                operation_id="write",
                kind="write",
                key="x",
                intended_version=1,
                write_id="w1",
                start_ns=0,
                end_ns=1_000_000,
            ),
            OperationRecord(
                operation_id="read",
                kind="read",
                key="x",
                observed_version=1,
                start_ns=2_000_000,
                end_ns=5_000_000,
            ),
        ],
        precondition={
            "status": "SATISFIED",
            "checks": [{"name": "fixture", "status": "SATISFIED"}],
        },
    )


class RQ4Tests(unittest.TestCase):
    def test_appendix_pairs_normal_and_adversarial_cells(self) -> None:
        rows = []
        for configuration in ("C5", "C6"):
            for property_name in ("RYW", "MR", "MW", "WFR"):
                rows.extend(
                    (
                        {
                            "scenario": "normal",
                            "configuration_id": configuration,
                            "property": property_name,
                            "p95_ms": "1.25",
                        },
                        {
                            "scenario": "rq1_fault",
                            "configuration_id": configuration,
                            "property": property_name,
                            "PASS": "2",
                            "VIOLATION": "1",
                            "UNAVAILABLE": "0",
                            "INDETERMINATE": "2",
                            "definitive_completion_rate": "0.6",
                            "p95_ms": "5.5",
                            "resolved_p95_ms": "2.5",
                        },
                    )
                )
        rendered = _metric_rows(rows)
        self.assertEqual(8, len(rendered.splitlines()))
        self.assertIn(
            "C5 & RYW & 1.25 & 2/1/0/2 & 60.0\\% & 5.50 & 2.50",
            rendered,
        )
        self.assertIn("NO DATA", _metric_rows(rows[:-1]))

    def test_metric_formulas_use_core_outcome_denominator(self) -> None:
        records = [
            {"configuration_id": "C5", "scenario": "normal", "property": "RYW", "outcome": "PASS", "latency_ms": 2.0, "critical_operation": "read"},
            {"configuration_id": "C5", "scenario": "normal", "property": "RYW", "outcome": "VIOLATION", "latency_ms": 4.0, "critical_operation": "read"},
            {"configuration_id": "C5", "scenario": "normal", "property": "RYW", "outcome": "INDETERMINATE", "latency_ms": 8.0, "critical_operation": "read"},
            {"configuration_id": "C5", "scenario": "normal", "property": "RYW", "outcome": "PRECONDITION_MISS", "latency_ms": None, "critical_operation": "read"},
        ]
        row = metric_rows(records)[0]
        self.assertEqual(3, row["attempted"])
        self.assertEqual(0.5, row["violation_rate"])
        self.assertEqual(2 / 3, row["definitive_completion_rate"])
        self.assertEqual(1 / 3, row["indeterminate_rate"])
        self.assertEqual(4.0, row["p50_ms"])
        self.assertEqual(7.6, row["p95_ms"])
        self.assertEqual(3.0, row["resolved_p50_ms"])
        self.assertEqual(3.9, row["resolved_p95_ms"])
        self.assertEqual(2, row["resolved_latency_n"])

    def test_loader_maps_experiment_and_rq2_partition_and_preserves_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "raw"
            normal_path = raw_root / "rq1" / "normal.json"
            partition_path = raw_root / "rq2" / "episodes" / "f3" / "partition.json"
            write_history(
                normal_path,
                _ryw_history(
                    campaign_id="experiment",
                    configuration_id="C5",
                    adversarial=False,
                ),
            )
            write_history(
                partition_path,
                _ryw_history(
                    campaign_id="rq2",
                    configuration_id="C6",
                    adversarial=True,
                    topology_condition="F3",
                ),
            )
            before = normal_path.read_bytes(), partition_path.read_bytes()
            records = load_records(raw_root)
            after = normal_path.read_bytes(), partition_path.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual({"normal", "partition"}, {record["scenario"] for record in records})
        self.assertEqual({"read"}, {record["critical_operation"] for record in records})
        self.assertEqual({3.0}, {record["latency_ms"] for record in records})

    def test_contrasts_and_fault_deltas_keep_missing_evidence_visible(self) -> None:
        rows = metric_rows(
            [
                {"configuration_id": "C5", "scenario": "normal", "property": "RYW", "outcome": "PASS", "latency_ms": 2.0, "critical_operation": "read"},
                {"configuration_id": "C6", "scenario": "normal", "property": "RYW", "outcome": "PASS", "latency_ms": 4.0, "critical_operation": "read"},
                {"configuration_id": "C6", "scenario": "partition", "property": "RYW", "outcome": "PASS", "latency_ms": 8.0, "critical_operation": "read"},
            ]
        )
        contrasts = contrast_rows(rows)
        normal = next(row for row in contrasts if row["scenario"] == "normal" and row["property"] == "RYW" and row["left_config"] == "C5" and row["right_config"] == "C6")
        partition = next(row for row in contrasts if row["scenario"] == "partition" and row["property"] == "RYW" and row["left_config"] == "C5" and row["right_config"] == "C6")
        self.assertEqual("CELL_PRESENT", normal["cell_status"])
        self.assertEqual("LATENCY_COMPLETE", normal["status"])
        self.assertEqual(2.0, normal["delta_p95_ms"])
        self.assertEqual("MISSING_CELL", partition["status"])
        deltas = fault_delta_rows(rows)
        self.assertEqual(1, len(deltas))
        self.assertEqual(4.0, deltas[0]["delta_p95_ms"])

    def test_partition_counts_keep_signature_cells_balanced(self) -> None:
        rows = metric_rows(
            [
                {"configuration_id": "C1", "scenario": "partition", "property": "RYW", "outcome": "VIOLATION", "latency_ms": 1.0},
                {"configuration_id": "C1", "scenario": "partition", "property": "MW", "outcome": "VIOLATION", "latency_ms": 1.0},
                {"configuration_id": "C1", "scenario": "partition", "property": "MR", "outcome": "PASS", "latency_ms": 1.0},
                {"configuration_id": "C6", "scenario": "partition", "property": "RYW", "outcome": "INDETERMINATE", "latency_ms": None},
                {"configuration_id": "C6", "scenario": "partition", "property": "MW", "outcome": "INDETERMINATE", "latency_ms": None},
            ]
        )
        counts = _partition_counts(rows)
        self.assertEqual(set(PARTITION_SIGNATURES), set(counts))
        self.assertEqual(1, counts[("C1", "RYW")]["VIOLATION"])
        self.assertEqual(1, counts[("C1", "MW")]["VIOLATION"])
        self.assertEqual(1, counts[("C6", "RYW")]["INDETERMINATE"])
        self.assertEqual(1, counts[("C6", "MW")]["INDETERMINATE"])

    def test_analyse_writes_csvs_and_figures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "raw"
            path = raw_root / "rq1" / "normal.json"
            write_history(path, _ryw_history(campaign_id="experiment", configuration_id="C5", adversarial=False))
            before = path.read_bytes()
            summary = analyse(
                raw_root=raw_root,
                summary_root=root / "summary/rq4",
                figures_root=root / "figures",
            )
            self.assertEqual("DATA", summary["status"])
            self.assertEqual(before, path.read_bytes())
            self.assertTrue((root / "summary/rq4/metrics.csv").is_file())
            self.assertTrue((root / "summary/rq4/contrasts.csv").is_file())
            self.assertTrue((root / "summary/rq4/fault_deltas.csv").is_file())
            for name in summary["figures"]:
                self.assertTrue((root / "figures" / name).read_bytes().startswith(b"%PDF-"))
            with (root / "summary/rq4/metrics.csv").open(newline="") as handle:
                self.assertEqual("C5", next(csv.DictReader(handle))["configuration_id"])
            self.assertEqual("DATA", json.loads((root / "summary/rq4/summary.json").read_text())["status"])


if __name__ == "__main__":
    unittest.main()
