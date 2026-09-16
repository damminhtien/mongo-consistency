from __future__ import annotations

import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.history import write_history
from mongo_consistency.models import History
from scripts.run_campaign import (
    CampaignShutdown,
    _campaign_manifest,
    _record_from_history,
    _write_json_atomic,
    adversarial_cases,
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
        self.assertEqual([], payload["runner_versions"])

    def test_sharded_manifest_uses_global_ordinal_holes(self) -> None:
        payload = _campaign_manifest(
            campaign="experiment",
            seed_base=20260915,
            runtime_metadata={
                "software_versions": {},
                "image_digest": [],
                "prediction_commit": "commit",
                "prediction_manifest_hash": "hash",
            },
            records_by_ordinal={8: {"trial_id": "eight", "ordinal": 8}},
            expected_case_count=2,
            planned_ordinals=[4, 8],
            global_expected_case_count=8,
            shard_index=1,
            shard_count=2,
            started_ns=1,
            status="RUNNING",
            resumed=True,
        )
        self.assertEqual([4, 8], payload["planned_ordinals"])
        self.assertEqual(4, payload["next_ordinal"])
        self.assertEqual(8, payload["global_expected_case_count"])
        self.assertEqual(1, payload["shard_index"])
        self.assertEqual(2, payload["shard_count"])

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
                    ),
                )
            manifest = run_campaign("experiment", raw_root, resume=True)
        self.assertEqual("COMPLETE", manifest["status"])
        self.assertEqual(1280, manifest["completed_case_count"])


if __name__ == "__main__":
    unittest.main()
