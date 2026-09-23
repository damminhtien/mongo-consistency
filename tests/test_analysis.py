from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.analysis import analyse, load_rows
from mongo_consistency.figures import representative_trace
from mongo_consistency.history import write_history
from mongo_consistency.models import History, OperationRecord


class AnalysisTests(unittest.TestCase):
    def test_raw_loader_ignores_histories_without_a_campaign_manifest(self) -> None:
        history = History(
            manifest={
                "trial_id": "experiment-00001-C1-ryw",
                "campaign_id": "experiment",
                "configuration_id": "C1",
                "property": "RYW",
                "adversarial": True,
            },
            operations=[],
            precondition={
                "status": "PRECONDITION_MISS",
                "checks": [{"name": "fixture", "status": "PRECONDITION_MISS"}],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory) / "raw"
            write_history(raw_root / "experiment/experiment-00001-C1-ryw.json", history)
            (raw_root / "experiment/campaign-manifest.json").write_text("{}", encoding="utf-8")
            write_history(raw_root / "experiment-rebuild/orphaned-history.json", history)

            rows = load_rows(raw_root)

        self.assertEqual(1, len(rows))
        self.assertEqual("experiment/experiment-00001-C1-ryw.json", rows[0]["path"])
        self.assertEqual("experiment", rows[0]["campaign_id"])

    def test_representative_trace_renders_c6_causal_read_timeout(self) -> None:
        row = {
            "path": "experiment/experiment-00018-C6-ryw.json",
            "history_hash": "c9ee334640ca60faaef80f5151b2560a951dadfb0553e48944707124046c9ffa",
            "configuration_id": "C6",
            "property": "RYW",
            "adversarial": True,
            "outcome": "UNAVAILABLE",
            "trace": [
                {
                    "operation_id": "write",
                    "status": "SUCCESS",
                    "write_concern": "majority",
                    "operation_time_after": {"seconds": 1789885744, "increment": 2},
                    "actual_server_address": "mongo3:27017",
                    "actual_role": "PRIMARY",
                },
                {
                    "operation_id": "read",
                    "status": "UNAVAILABLE",
                    "causal_session": True,
                    "read_concern": "majority",
                    "after_cluster_time": {"seconds": 1789885744, "increment": 2},
                    "error_code": "NetworkTimeout",
                    "response_received": False,
                    "duration_ms": 5000.0,
                    "actual_server_address": "mongo1:27017",
                    "actual_role": "SECONDARY",
                },
            ],
            "fault_events": [
                {"action": "isolate", "members": ["mongo1"], "status": "APPLIED"},
                {
                    "action": "heal",
                    "status": "APPLIED",
                    "stable_topology": {"stable": True},
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = representative_trace(Path(directory) / "trace.pdf", [row])
            self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
            if shutil.which("pdftotext"):
                body = subprocess.check_output(["pdftotext", str(path), "-"], text=True)
                self.assertIn("afterClusterTime: (1789885744, 2)", body)
                self.assertIn("NetworkTimeout after 5.00 s", body)
                self.assertIn("UNAVAILABLE", body)

    def test_empty_analysis_is_explicitly_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = analyse(
                raw_root=root / "raw",
                summary_root=root / "summary",
                figures_root=root / "figures",
            )
            self.assertEqual("NO_DATA", summary["status"])
            self.assertEqual(0, summary["history_count"])
            self.assertEqual({}, summary["campaign_summaries"])
            self.assertTrue((root / "summary/summary.json").is_file())
            self.assertTrue((root / "figures/outcome-heatmap.pdf").read_bytes().startswith(b"%PDF-"))

    def test_analysis_rebuilds_counts_from_raw_history(self) -> None:
        history = History(
            manifest={
                "trial_id": "normal-00001-C1-ryw",
                "campaign_id": "normal",
                "configuration_id": "C1",
                "property": "RYW",
                "adversarial": False,
            },
            operations=[
                OperationRecord(
                    operation_id="write",
                    kind="write",
                    key="x",
                    session_id="session-fixture",
                    intended_version=1,
                    write_id="w1",
                    start_ns=0,
                    end_ns=1_000_000,
                ),
                OperationRecord(
                    operation_id="read",
                    kind="read",
                    key="x",
                    session_id="session-fixture",
                    observed_version=1,
                    start_ns=1_000_000,
                    end_ns=3_000_000,
                ),
            ],
            precondition={
                "status": "SATISFIED",
                "checks": [{"name": "fixture-state", "status": "SATISFIED"}],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_history(root / "raw/normal/trial.json", history)
            summary = analyse(
                raw_root=root / "raw",
                summary_root=root / "summary",
                figures_root=root / "figures",
            )
            self.assertEqual("DATA", summary["status"])
            self.assertEqual(1, summary["history_count"])
            self.assertEqual(1, summary["outcome_counts"]["PASS"])
            self.assertEqual(1.0, summary["groups"][0]["operation_success_rate"])
            self.assertEqual(1.5, summary["groups"][0]["latency_ms"]["p50"])
            self.assertEqual(1, summary["campaign_summaries"]["normal"]["normal"]["history_count"])
            self.assertEqual(0, summary["campaign_summaries"]["normal"]["adversarial"]["history_count"])
            parsed = json.loads((root / "summary/summary.json").read_text())
            self.assertEqual("DATA", parsed["status"])
            for name in ("architecture", "property-timelines", "prediction-observation", "representative-trace"):
                self.assertTrue((root / "figures" / f"{name}.pdf").read_bytes().startswith(b"%PDF-"))

    def test_analysis_keeps_fault_durations_and_factorial_metrics(self) -> None:
        history = History(
            manifest={
                "trial_id": "experiment-00001-C1-ryw",
                "campaign_id": "experiment",
                "configuration_id": "C1",
                "property": "RYW",
                "adversarial": True,
            },
            operations=[
                OperationRecord(
                    operation_id="write",
                    kind="write",
                    key="x",
                    session_id="session-fixture",
                    intended_version=1,
                    write_id="w1",
                    start_ns=0,
                    end_ns=1_000_000,
                ),
                OperationRecord(
                    operation_id="read",
                    kind="read",
                    key="x",
                    session_id="session-fixture",
                    observed_version=1,
                    start_ns=1_000_000,
                    end_ns=2_000_000,
                ),
            ],
            precondition={
                "status": "SATISFIED",
                "checks": [{"name": "fixture-state", "status": "SATISFIED"}],
            },
            fault_events=[
                {
                    "event_id": "fault",
                    "action": "isolate",
                    "members": ["mongo2"],
                    "start_ns": 10,
                    "status": "APPLIED",
                    "election_start_ns": 100,
                    "election_end_ns": 2_100_100,
                },
                {
                    "event_id": "heal",
                    "action": "heal",
                    "members": ["mongo2"],
                    "start_ns": 3_000_000,
                    "status": "APPLIED",
                    "recovery_start_ns": 3_000_000,
                    "recovery_end_ns": 5_000_100,
                },
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_history(root / "raw/experiment/trial.json", history)
            summary = analyse(
                raw_root=root / "raw",
                summary_root=root / "summary",
                figures_root=root / "figures",
            )
            latency_pdf = root / "figures/latency.pdf"
            self.assertTrue(latency_pdf.read_bytes().startswith(b"%PDF-"))
            latency_text = (
                subprocess.check_output(["pdftotext", str(latency_pdf), "-"], text=True)
                if shutil.which("pdftotext")
                else ""
            )
        self.assertEqual(2.1, summary["overall"]["election_ms"]["p50"])
        self.assertEqual(2.0001, summary["overall"]["recovery_ms"]["p50"])
        self.assertEqual(1, summary["campaign_summaries"]["experiment"]["adversarial"]["history_count"])
        self.assertEqual(
            1,
            summary["campaign_summaries"]["experiment"]["properties"]["RYW"]["history_count"],
        )
        self.assertIn("metrics", summary["factorial"]["properties"]["RYW"])
        if latency_text:
            self.assertIn("Normal control", latency_text)
            self.assertIn("Adversarial", latency_text)
            self.assertIn("log scale", latency_text)


if __name__ == "__main__":
    unittest.main()
