from __future__ import annotations

import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.history import write_history
from mongo_consistency.models import History
from scripts.run_campaign import (
    CampaignShutdown,
    _campaign_manifest,
    _record_from_history,
    _require_frozen_provenance,
    _smoke_gate,
    _write_json_atomic,
    adversarial_cases,
    campaign_plan,
    campaign_cases,
    load_configurations,
    load_json,
    run_campaign,
    trial_id_for,
)


class CampaignResumeTests(unittest.TestCase):
    def test_resume_record_requires_exact_case_identity(self) -> None:
        trial_id = trial_id_for("experiment", 7, "C3", "MW")
        history = History(
            manifest={
                "trial_id": trial_id,
                "campaign_id": "experiment",
                "configuration_id": "C3",
                "property": "MW",
                "adversarial": True,
                "seed": 20260922,
            },
            operations=[],
            precondition={
                "status": "SATISFIED",
                "checks": [{"name": "resume-fixture", "status": "SATISFIED"}],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{trial_id}.json"
            write_history(path, history)
            record = _record_from_history(
                path,
                campaign="experiment",
                ordinal=7,
                configuration_id="C3",
                property_name="MW",
                adversarial=True,
                seed=20260922,
            )
        self.assertEqual(7, record["ordinal"])
        self.assertEqual(trial_id, record["trial_id"])

    def test_partial_manifest_preserves_holes_and_next_ordinal(self) -> None:
        payload = _campaign_manifest(
            campaign="experiment",
            seed_base=20260915,
            runtime_metadata={
                "software_versions": {},
                "image_digest": [],
                "prediction_commit": "commit",
                "prediction_manifest_hash": "hash",
            },
            records_by_ordinal={3: {"trial_id": "three", "ordinal": 3}},
            expected_case_count=5,
            started_ns=1,
            status="INTERRUPTED",
            resumed=True,
            finished_ns=2,
        )
        self.assertEqual(1, payload["case_count"])
        self.assertEqual(1, payload["next_ordinal"])
        self.assertEqual("INTERRUPTED", payload["status"])
        self.assertEqual([], payload["runner_commits"])

    def test_smoke_plan_covers_every_configuration_and_property_once(self) -> None:
        configurations = load_configurations(ROOT / "configs/configurations.json")
        campaign_config = load_json(ROOT / "configs/campaign.json")
        plan = campaign_plan("smoke", configurations, campaign_config)
        self.assertEqual(32, len(plan))
        self.assertTrue(all(adversarial for _, _, _, adversarial in plan))
        self.assertEqual(
            {property_name: 8 for property_name in ("RYW", "MR", "MW", "WFR")},
            {
                property_name: sum(case[2] == property_name for case in plan)
                for property_name in ("RYW", "MR", "MW", "WFR")
            },
        )
        self.assertEqual(
            {f"C{number}" for number in range(1, 9)},
            {configuration_id for _, configuration_id, _, _ in plan},
        )

    def test_smoke_gate_requires_complete_coverage_and_low_miss_rate(self) -> None:
        records = {
            ordinal: {
                "property": ("RYW", "MR", "MW", "WFR")[(ordinal - 1) // 8],
                "precondition_status": "SATISFIED",
                "outcome": "PASS",
            }
            for ordinal in range(1, 33)
        }
        self.assertTrue(_smoke_gate(records)["passed"])

        records[1]["precondition_status"] = "PRECONDITION_MISS"
        gate = _smoke_gate(records)
        self.assertFalse(gate["passed"])
        self.assertEqual(0.125, gate["precondition_miss_rate_by_property"]["RYW"])

        del records[1]
        self.assertTrue(any("coverage is incomplete" in item for item in _smoke_gate(records)["failures"]))

    def test_smoke_gate_rejects_harness_errors(self) -> None:
        records = {
            ordinal: {
                "property": ("RYW", "MR", "MW", "WFR")[(ordinal - 1) // 8],
                "precondition_status": "SATISFIED",
                "outcome": "HARNESS_ERROR" if ordinal == 1 else "PASS",
            }
            for ordinal in range(1, 33)
        }
        gate = _smoke_gate(records)
        self.assertFalse(gate["passed"])
        self.assertTrue(any("HARNESS_ERROR" in item for item in gate["failures"]))

    def test_only_smoke_may_run_without_frozen_provenance(self) -> None:
        _require_frozen_provenance("smoke", {})
        with self.assertRaisesRegex(ValueError, "committed, frozen"):
            _require_frozen_provenance("pilot", {})

    def test_manifest_write_replaces_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign-manifest.json"
            _write_json_atomic(path, {"status": "RUNNING"})
            self.assertEqual('{\n  "status": "RUNNING"\n}\n', path.read_text())
            self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_first_signal_is_graceful_and_second_signal_is_forceful(self) -> None:
        shutdown = CampaignShutdown()
        shutdown._handle(signal.SIGINT, None)
        self.assertTrue(shutdown.requested)
        with self.assertRaises(KeyboardInterrupt):
            shutdown._handle(signal.SIGINT, None)
        self.assertTrue(shutdown.force_requested)

    def test_resume_skips_all_validated_histories_without_database_access(self) -> None:
        configurations = load_configurations(ROOT / "configs/configurations.json")
        campaign_config = load_json(ROOT / "configs/campaign.json")
        cases = [
            (configuration_id, property_name, False)
            for configuration_id, property_name in campaign_cases(
                "experiment", configurations, campaign_config
            )
        ]
        cases.extend(
            (configuration_id, property_name, True)
            for configuration_id, property_name in adversarial_cases(
                "experiment", configurations, campaign_config
            )
        )
        import random

        random.Random(int(campaign_config["seed_base"])).shuffle(cases)
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory) / "raw"
            for ordinal, (configuration_id, property_name, adversarial) in enumerate(cases, start=1):
                trial_id = trial_id_for("experiment", ordinal, configuration_id, property_name)
                write_history(
                    raw_root / "experiment" / f"{trial_id}.json",
                    History(
                        manifest={
                            "trial_id": trial_id,
                            "campaign_id": "experiment",
                            "configuration_id": configuration_id,
                            "property": property_name,
                            "adversarial": adversarial,
                            "seed": int(campaign_config["seed_base"]) + ordinal,
                        },
                        operations=[],
                        precondition={
                            "status": "SATISFIED",
                            "checks": [{"name": "resume-fixture", "status": "SATISFIED"}],
                        },
                    ),
                )
            runtime_metadata = {
                "software_versions": {},
                "image_digest": [],
                "prediction_commit": "prediction-commit",
                "prediction_manifest_hash": "prediction-hash",
                "protocol_commit": "protocol-commit",
                "protocol_hash": "protocol-hash",
                "runner_commit": "runner-commit",
                "runner_dirty": False,
                "seed_base": int(campaign_config["seed_base"]),
            }
            with patch(
                "scripts.run_campaign.campaign_runtime_metadata",
                return_value=runtime_metadata,
            ):
                manifest = run_campaign("experiment", raw_root, resume=True)
        self.assertEqual("COMPLETE", manifest["status"])
        self.assertEqual(1280, manifest["completed_case_count"])


if __name__ == "__main__":
    unittest.main()
