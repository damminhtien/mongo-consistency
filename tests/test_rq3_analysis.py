from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import analyse_rq3

from mongo_consistency.config import load_configurations
from mongo_consistency.history import write_history
from mongo_consistency.models import History
from mongo_consistency.rq3 import TOPOLOGY_PLANS, pair_control

FIXTURE_PATH = ROOT / "tests/fixtures/rq3/protocol-v2-pairs.json"
CONTRASTS = {
    "M1": ("rq3-m1", ("C5", "C6"), "RYW", ("write", "read")),
    "M2": ("rq3-m2", ("C8", "C5"), "WFR", ("read", "write")),
    "M3": ("rq3-m3", ("C3", "C6"), "MW", ("first_write", "second_write")),
}
PROPERTIES = {"M1": "RYW", "M2": "WFR", "M3": "MW"}
CONFIGURATIONS = load_configurations(ROOT / "configs/configurations.json")


def _fixture_pairs() -> dict[str, dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["pairs"]


def _frozen_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RQ3AnalysisTests(unittest.TestCase):
    def test_m1_reports_stale_read_and_causal_bound_as_recorded(self) -> None:
        pair = _fixture_pairs()["M1"]
        histories = pair["histories"]
        row = analyse_rq3._row_for_pair(
            "M1", pair["pair_id"], histories, CONFIGURATIONS
        )
        counts = analyse_rq3._counts([row], "M1")

        self.assertTrue(row["control_valid"])
        self.assertEqual(1, counts["C5_stale_success_without_after_cluster_time"])
        self.assertEqual(1, counts["C5_write_time_ahead_of_routed_member_last_write"])
        self.assertEqual(1, counts["C6_after_cluster_time_matches_write_time"])
        self.assertEqual(1, counts["C6_after_cluster_time_ahead_of_routed_member_last_write"])
        self.assertEqual(1, counts["C6_unavailable_reads"])

    def test_m2_setup_write_event_proves_w1_route(self) -> None:
        pair = _fixture_pairs()["M2"]
        control = pair_control(
            "M2", pair["pair_id"], pair["histories"],
            configurations=CONFIGURATIONS,
        )

        setup = control["observed"]["C8"]["observed"]["setup_write"]
        self.assertTrue(control["control_valid"])
        self.assertEqual("mongo3", setup["member"])
        self.assertEqual({"w": 1}, setup["write_concern"])
        self.assertTrue(setup["command_started"])

    def test_m3_keeps_acknowledgement_timeout_and_final_presence_distinct(self) -> None:
        pair = _fixture_pairs()["M3"]
        row = analyse_rq3._row_for_pair(
            "M3", pair["pair_id"], pair["histories"], CONFIGURATIONS
        )
        counts = analyse_rq3._counts([row], "M3")

        self.assertTrue(row["control_valid"])
        self.assertEqual(1, counts["C3_w1_first_write_acknowledged"])
        self.assertEqual(1, counts["C3_acknowledged_w1_absent_from_all_converged_members"])
        self.assertEqual(1, counts["C6_majority_first_write_network_timeout"])
        self.assertEqual(1, counts["C6_w1_present_on_all_final_members_after_timeout"])

    def test_consistency_outcomes_do_not_change_control_validity(self) -> None:
        pair = copy.deepcopy(_fixture_pairs()["M2"])
        histories = pair["histories"]
        histories["C8"]["result"] = "VIOLATION"
        histories["C5"]["result"] = "PASS"

        control = pair_control(
            "M2", pair["pair_id"], histories, configurations=CONFIGURATIONS
        )

        self.assertTrue(control["control_valid"])
        histories["C8"]["operations"][0]["actual_server_address"] = "mongo2:27017"
        invalid = pair_control(
            "M2", pair["pair_id"], histories, configurations=CONFIGURATIONS
        )
        self.assertFalse(invalid["control_valid"])
        self.assertIn("C8: read route is 'mongo2', expected mongo3", invalid["invalid_reasons"])

        wrong_configuration = copy.deepcopy(_fixture_pairs()["M2"])
        wrong_configuration["histories"]["C8"]["manifest"]["causal_session"] = True
        invalid_settings = pair_control(
            "M2",
            wrong_configuration["pair_id"],
            wrong_configuration["histories"],
            configurations=CONFIGURATIONS,
        )
        self.assertFalse(invalid_settings["control_valid"])
        self.assertIn(
            "C8: causal_session differs from the registered configuration",
            invalid_settings["invalid_reasons"],
        )

    def test_summary_excludes_invalid_pair_and_selects_first_valid_pair(self) -> None:
        fixtures = _fixture_pairs()
        with tempfile.TemporaryDirectory(prefix="rq3-analysis-") as tmp:
            work_root = Path(tmp)
            raw_root = work_root / "raw/rq3"
            preflight_path = work_root / "raw/rq3-preflight.json"
            output_root = work_root / "analysis/rq3"
            submission_root = work_root / "submission"
            preflight_path.parent.mkdir(parents=True)
            preflight_path.write_text(
                json.dumps(
                    {
                        "schema_version": "rq3-preflight.v2",
                        "protocol_id": "rq3-protocol.v2",
                        "status": "PASS",
                        "planned_cycle_count": 10,
                        "completed_cycle_count": 10,
                        "passed_cycle_count": 10,
                        "topology_plan": TOPOLOGY_PLANS["M3"].to_dict(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            runner_commit = "fixture-runner-commit"
            protocol_hash = _frozen_hash(ROOT / "docs/experimental-protocol.md")
            prediction_hash = _frozen_hash(ROOT / "configs/predictions.json")
            software_versions = {"python": "3.14.fixture", "pymongo": "fixture"}
            image_digest = "sha256:" + "a" * 64
            checker_version = "checker-fixture"
            protocol_commit = "fixture-protocol-commit"
            prediction_commit = "fixture-prediction-commit"
            provenance = {
                "runner_commit": runner_commit,
                "runner_dirty": False,
                "protocol_hash": protocol_hash,
                "prediction_manifest_hash": prediction_hash,
                "software_versions": software_versions,
                "image_digest": image_digest,
                "checker_version": checker_version,
                "protocol_commit": protocol_commit,
                "prediction_commit": prediction_commit,
            }
            records = []
            pair_controls = []
            ordinal = 0
            for contrast_id, (campaign_id, configurations, property_name, _operations) in CONTRASTS.items():
                for replicate in range(1, 9):
                    pair_id = f"{contrast_id.lower()}-r{replicate:02d}"
                    pair_seed = 700 + replicate
                    histories = {}
                    for configuration_id in configurations:
                        ordinal += 1
                        fixture_pair = fixtures[contrast_id]
                        payload = copy.deepcopy(fixture_pair["histories"][configuration_id])
                        trial_id = f"{campaign_id}-{ordinal:05d}-{configuration_id.lower()}-{property_name.lower()}"
                        manifest = payload["manifest"]
                        manifest.update(
                            {
                                "trial_id": trial_id,
                                "campaign_id": campaign_id,
                                "configuration_id": configuration_id,
                                "property": property_name,
                                "seed": pair_seed,
                                "rq3_pair_id": pair_id,
                                "rq3_pair_seed": pair_seed,
                                "rq3_replicate": replicate,
                                "schedule_id": f"fixture-schedule-{replicate:02d}",
                                "runner_commit": runner_commit,
                                "runner_dirty": False,
                                "protocol_commit": protocol_commit,
                                "protocol_hash": protocol_hash,
                                "prediction_commit": prediction_commit,
                                "prediction_manifest_hash": prediction_hash,
                                "software_versions": software_versions,
                                "image_digest": image_digest,
                                "checker_version": checker_version,
                            }
                        )
                        history = History.from_dict(payload)
                        if contrast_id == "M1" and replicate == 1 and configuration_id == "C6":
                            next(
                                operation
                                for operation in history.operations
                                if operation.operation_id == "read"
                            ).actual_server_address = "mongo2:27017"
                        history_path = raw_root / campaign_id / f"{trial_id}.json"
                        history_hash = write_history(history_path, history)
                        history_dict = history.to_dict()
                        histories[configuration_id] = history_dict
                        record = {
                            "trial_id": trial_id,
                            "ordinal": ordinal,
                            "campaign": campaign_id,
                            "contrast_id": contrast_id,
                            "pair_id": pair_id,
                            "replicate": replicate,
                            "pair_seed": pair_seed,
                            "configuration_id": configuration_id,
                            "property": property_name,
                            "adversarial": True,
                            "seed": pair_seed,
                            "path": history_path.relative_to(work_root).as_posix(),
                            "history_hash": history_hash,
                            "outcome": history.metadata.get("fixture_consistency_outcome", "PASS"),
                            "precondition_status": "SATISFIED",
                            "runner_commit": runner_commit,
                            "runner_error": None,
                        }
                        records.append(record)
                    pair_controls.append(
                        pair_control(
                            contrast_id,
                            pair_id,
                            histories,
                            configurations=CONFIGURATIONS,
                        )
                    )

            manifest_path = raw_root / "campaign-manifest.json"
            manifest = {
                "schema_version": "rq3-campaign.v2",
                "protocol_id": "rq3-protocol.v2",
                "campaign": "rq3",
                "status": "COMPLETE",
                "repetitions_per_contrast": 8,
                "seed_base": 700,
                "planned_case_count": 48,
                "completed_case_count": 48,
                "runner_commit": runner_commit,
                "runner_script_sha256": _frozen_hash(ROOT / "scripts/run_rq3_campaign.py"),
                "configuration_sha256": _frozen_hash(ROOT / "configs/configurations.json"),
                "protocol_sha256": protocol_hash,
                "topology_plan_sha256": _frozen_hash(ROOT / "src/mongo_consistency/rq3.py"),
                "preflight_sha256": _frozen_hash(preflight_path),
                "anchor_manifest_sha256": _frozen_hash(ROOT / "configs/rq3-anchors.json"),
                "runtime_provenance": provenance,
                "contrasts": [],
                "pair_controls": pair_controls,
                "records": records,
                "started_ns": 1,
                "last_updated_ns": 2,
                "finished_ns": 3,
            }
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

            summary = analyse_rq3._summary_and_report(
                input_root=raw_root,
                output_root=output_root,
                submission_root=submission_root,
            )
            generated_paths = (
                output_root / "summary.json",
                output_root / "selection-manifest.json",
                submission_root / "generated-rq3.tex",
                submission_root / "figures/rq3-causal-timeline.pdf",
            )
            first_outputs = [path.read_bytes() for path in generated_paths]
            second_summary = analyse_rq3._summary_and_report(
                input_root=raw_root,
                output_root=output_root,
                submission_root=submission_root,
            )
            second_outputs = [path.read_bytes() for path in generated_paths]
            self.assertEqual(summary, second_summary)
            self.assertEqual(first_outputs, second_outputs)

            m1 = summary["contrast_summaries"]["M1"]
            self.assertEqual("rq3-analysis.v2", summary["schema_version"])
            self.assertEqual(7, m1["control_valid_pair_count"])
            self.assertEqual(1, m1["invalid_pair_count"])
            self.assertEqual("m1-r01", m1["invalid_pairs"][0]["pair_id"])
            self.assertEqual(
                7,
                m1["signature_counts"]["C5_stale_success_without_after_cluster_time"],
            )
            self.assertEqual("m1-r02", m1["selected_pair"]["pair_id"])
            self.assertEqual("m2-r01", summary["contrast_summaries"]["M2"]["selected_pair"]["pair_id"])
            self.assertEqual("m3-r01", summary["contrast_summaries"]["M3"]["selected_pair"]["pair_id"])
            selection = json.loads((output_root / "selection-manifest.json").read_text())
            report = (submission_root / "generated-rq3.tex").read_text(encoding="utf-8")
            self.assertEqual("rq3-selection.v2", selection["schema_version"])
            self.assertEqual(summary["preflight_sha256"], selection["preflight_sha256"])
            self.assertIn("Control-valid pairs 7/8", report)
            self.assertNotIn("post-hoc", report)
            self.assertNotIn("permutation", report)
            self.assertEqual(48, len(manifest["records"]))


if __name__ == "__main__":
    unittest.main()
