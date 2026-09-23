from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.history import write_history
from mongo_consistency.models import History, OperationRecord
from scripts.run_campaign import _campaign_manifest
from scripts.validate_records import (
    SCHEMA_NAMES,
    _check_schema_instance,
    _check_rq3_campaign_manifest_v2,
    _load_validators,
)


def valid_history() -> History:
    return History(
        manifest={
            "schema_version": "manifest.v1",
            "trial_id": "smoke-00001-C1-ryw",
            "campaign_id": "smoke",
            "configuration_id": "C1",
            "read_concern": "local",
            "write_concern": "w:1",
            "causal_session": False,
            "property": "RYW",
            "schedule_id": "ryw-stale-secondary",
            "seed": 20260916,
            "namespace": {
                "database": "mc_smoke_00001",
                "collection": "logical",
                "document_id": "smoke-00001/x",
            },
            "timeout_policy": {
                "connect_ms": 1000,
                "server_selection_ms": 2000,
                "operation_ms": 5000,
                "write_concern_ms": 2000,
                "setup_write_concern_ms": 15000,
                "setup_socket_ms": 17000,
                "election_barrier_ms": 30000,
                "subtrial_ms": 60000,
            },
        },
        operations=[
            OperationRecord(
                operation_id="read-1",
                kind="read",
                key="x",
                operation_status="SUCCESS",
                command_started=True,
                response_received=True,
                observed_document_exists=False,
            )
        ],
        precondition={
            "status": "SATISFIED",
            "checks": [{"name": "initial-state", "status": "SATISFIED"}],
        },
        fault_events=[
            {
                "event_id": "fault-1",
                "action": "isolate",
                "members": ["mongo3"],
                "start_ns": 1,
                "applied_ns": 2,
                "status": "APPLIED",
                "responses": [],
            }
        ],
    )


class SchemaValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema_errors: list[str] = []
        cls.validators = _load_validators(ROOT, cls.schema_errors)

    def test_every_schema_is_meta_valid_and_registered(self) -> None:
        self.assertEqual([], self.schema_errors)
        self.assertEqual(set(SCHEMA_NAMES), set(self.validators))

    def test_serialized_history_resolves_nested_schemas(self) -> None:
        history = valid_history()
        with self.subTest("serialization contract"):
            import tempfile

            with tempfile.TemporaryDirectory() as directory:
                write_history(Path(directory) / "history.json", history)
                payload = history.to_dict()
        errors: list[str] = []
        _check_schema_instance(
            payload,
            self.validators["history.v1.json"],
            "history",
            errors,
        )
        self.assertEqual([], errors)

    def test_history_schema_rejects_invalid_nested_manifest_and_unknown_operation_fields(self) -> None:
        history = valid_history()
        payload = history.to_dict()
        payload["history_hash"] = "0" * 64
        payload["manifest"]["read_concern"] = "linearizable"
        payload["operations"][0]["document_version_before"] = 0
        errors: list[str] = []

        _check_schema_instance(
            payload,
            self.validators["history.v1.json"],
            "history",
            errors,
        )

        self.assertTrue(any("manifest.read_concern" in error for error in errors), errors)
        self.assertTrue(any("document_version_before" in error for error in errors), errors)

    def test_campaign_run_schema_rejects_unplanned_outcome_values(self) -> None:
        record = {
            "trial_id": "experiment-00001-C1-ryw",
            "ordinal": 1,
            "campaign": "experiment",
            "configuration_id": "C1",
            "property": "RYW",
            "adversarial": True,
            "seed": 20260916,
            "path": "results/raw/experiment/trial.json",
            "history_hash": "0" * 64,
            "outcome": "PASS",
            "precondition_status": "SATISFIED",
            "runner_commit": "abc123",
            "runner_error": None,
        }
        campaign = _campaign_manifest(
            campaign="experiment",
            seed_base=20260915,
            runtime_metadata={
                "software_versions": {"python": "3.14.7"},
                "image_digest": None,
                "runner_commit": "abc123",
                "runner_dirty": False,
            },
            records_by_ordinal={1: record},
            expected_case_count=1,
            started_ns=1,
            planned_ordinals=[1],
            status="RUNNING",
            resumed=False,
        )
        errors: list[str] = []

        _check_schema_instance(
            campaign,
            self.validators["campaign-run.v1.json"],
            "campaign",
            errors,
        )
        self.assertEqual([], errors)

        campaign["records"][0]["outcome"] = "UNSUPPORTED"
        errors.clear()
        _check_schema_instance(
            campaign,
            self.validators["campaign-run.v1.json"],
            "campaign",
            errors,
        )

        self.assertTrue(any("UNSUPPORTED" in error for error in errors), errors)

    def test_checker_result_serialization_matches_outcome_schema(self) -> None:
        from mongo_consistency.checkers import check_history

        result = check_history(valid_history()).to_dict()
        errors: list[str] = []
        _check_schema_instance(
            result,
            self.validators["outcome.v1.json"],
            "outcome",
            errors,
        )
        self.assertEqual([], errors)

    def test_rq3_v2_manifest_validator_handles_repository_anchor_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs/configurations.json").write_text(
                (ROOT / "configs/configurations.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            manifest_path = root / "results/raw/rq3/campaign-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}", encoding="utf-8")

            errors: list[str] = []
            _check_rq3_campaign_manifest_v2(
                {
                    "protocol_id": "rq3-protocol.v2",
                    "repetitions_per_contrast": 8,
                    "records": [],
                },
                manifest_path,
                errors,
            )
            forged_control_errors: list[str] = []
            _check_rq3_campaign_manifest_v2(
                {
                    "protocol_id": "rq3-protocol.v2",
                    "repetitions_per_contrast": 8,
                    "records": [],
                    "pair_controls": [
                        {
                            "contrast_id": "M1",
                            "pair_id": "m1-r01",
                            "pair_seed": 701,
                            "control_valid": True,
                            "invalid_reasons": [],
                        }
                    ],
                },
                manifest_path,
                forged_control_errors,
            )

        self.assertTrue(any("cannot verify RQ3 historical anchors" in error for error in errors), errors)
        self.assertTrue(
            any("pair control m1-r01 differs from raw-history recomputation" in error
                for error in forged_control_errors),
            forged_control_errors,
        )


if __name__ == "__main__":
    unittest.main()
