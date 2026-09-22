from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations, load_predictions
from mongo_consistency.history import (
    compute_history_hash,
    read_history,
    validate_history,
    write_history,
)
from mongo_consistency.models import History, OperationRecord, Outcome
from mongo_consistency.trial import classify_exception


def operation(operation_id: str, kind: str, key: str = "x", **kwargs: object) -> OperationRecord:
    kwargs.setdefault("session_id", "session-fixture")
    return OperationRecord(operation_id=operation_id, kind=kind, key=key, **kwargs)


def base_history(
    property_name: str,
    operations: list[OperationRecord],
    *,
    final_observation: dict[str, object] | None = None,
) -> History:
    return History(
        manifest={"trial_id": "fixture-1", "property": property_name},
        operations=operations,
        precondition={
            "status": "SATISFIED",
            "checks": [{"name": "fixture-state", "status": "SATISFIED"}],
        },
        final_observation=final_observation,
    )


def final_observation(updates: list[dict[str, object]]) -> dict[str, object]:
    state = {
        "reachable": True,
        "exists": True,
        "observation_valid": True,
        "updates": updates,
    }
    return {
        "converged": True,
        "topology": {
            "stable": True,
            "primary": "mongo1",
            "secondaries": ["mongo2", "mongo3"],
        },
        "members": {member: dict(state) for member in ("mongo1", "mongo2", "mongo3")},
    }


class ConfigurationTests(unittest.TestCase):
    def test_full_factorial_matrix_and_predictions_are_loadable(self) -> None:
        configurations = load_configurations(ROOT / "configs/configurations.json")
        predictions = load_predictions(ROOT / "configs/predictions.json")
        self.assertEqual(tuple(f"C{number}" for number in range(1, 9)), tuple(configurations))
        self.assertEqual(tuple(configurations), tuple(predictions))


class CheckerTests(unittest.TestCase):
    def test_operation_serializes_empty_write_preimage_fields(self) -> None:
        payload = operation("read", "read").to_dict()

        self.assertEqual([], payload["write_base_write_ids"])
        self.assertFalse(payload["write_base_observed"])
        self.assertEqual((), OperationRecord.from_dict(payload).write_base_write_ids)

    def test_ryw_pass_and_violation(self) -> None:
        passing = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=2, write_id="w1"),
                operation("read", "read", observed_version=2),
            ],
        )
        violating = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=2, write_id="w1"),
                operation("read", "read", observed_version=1),
            ],
        )
        self.assertEqual(Outcome.PASS, check_history(passing).outcome)
        self.assertEqual(Outcome.VIOLATION, check_history(violating).outcome)

    def test_mr_detects_decreasing_successive_read(self) -> None:
        history = base_history(
            "MR",
            [
                operation("first_read", "read", observed_version=4),
                operation("second_read", "read", observed_version=3),
            ],
        )
        result = check_history(history)
        self.assertEqual(Outcome.VIOLATION, result.outcome)

    def test_client_centric_properties_require_one_process_session(self) -> None:
        ryw = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read", observed_version=1, session_id="other-session"),
            ],
        )
        mr = base_history(
            "MR",
            [
                operation("first_read", "read", observed_version=1),
                operation("second_read", "read", observed_version=1, session_id="other-session"),
            ],
        )
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(ryw).outcome)
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(mr).outcome)

    def test_mw_passes_when_predecessor_is_in_write_preimage(self) -> None:
        history = base_history(
            "MW",
            [
                operation(
                    "first_write",
                    "write",
                    intended_version=1,
                    write_id="w1",
                ),
                operation(
                    "second_write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="w1",
                    write_base_observed=True,
                    write_base_write_ids=("init", "w1"),
                ),
            ],
        )
        self.assertEqual(Outcome.PASS, check_history(history).outcome)

    def test_mw_requires_one_logical_key(self) -> None:
        history = base_history(
            "MW",
            [
                operation("first_write", "write", intended_version=1, write_id="w1"),
                operation(
                    "second_write",
                    "write",
                    key="y",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="w1",
                    write_base_observed=True,
                    write_base_write_ids=("init", "w1"),
                ),
            ],
        )
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(history).outcome)

    def test_mw_detects_missing_predecessor_in_write_preimage(self) -> None:
        history = base_history(
            "MW",
            [
                operation(
                    "first_write",
                    "write",
                    intended_version=1,
                    write_id="w1",
                ),
                operation(
                    "second_write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="w1",
                    write_base_observed=True,
                    write_base_write_ids=("init",),
                ),
            ],
        )
        self.assertEqual(Outcome.VIOLATION, check_history(history).outcome)

    def test_mw_rejects_wrong_parent_dependency(self) -> None:
        history = base_history(
            "MW",
            [
                operation("first_write", "write", intended_version=1, write_id="w1"),
                operation(
                    "second_write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="wrong-write",
                    write_base_observed=True,
                    write_base_write_ids=("init", "w1"),
                ),
            ],
        )
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(history).outcome)

    def test_mw_without_write_preimage_is_indeterminate(self) -> None:
        history = base_history(
            "MW",
            [
                operation("first_write", "write", intended_version=1, write_id="w1"),
                operation(
                    "second_write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="w1",
                ),
            ],
        )
        self.assertEqual(Outcome.INDETERMINATE, check_history(history).outcome)

    def test_mw_does_not_use_client_wall_clock_order_as_oracle(self) -> None:
        history = base_history(
            "MW",
            [
                operation(
                    "first_write",
                    "write",
                    intended_version=1,
                    write_id="w1",
                    start_ns=10,
                    end_ns=20,
                ),
                operation(
                    "second_write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="w1",
                    start_ns=100,
                    end_ns=110,
                    write_base_observed=True,
                    write_base_write_ids=("init",),
                ),
            ],
        )
        self.assertEqual(Outcome.VIOLATION, check_history(history).outcome)

    def test_mw_does_not_use_post_heal_snapshot(self) -> None:
        history = base_history(
            "MW",
            [
                operation("first_write", "write", intended_version=1, write_id="w1"),
                operation(
                    "second_write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    parent_write_id="w1",
                    write_base_observed=True,
                    write_base_write_ids=("init", "w1"),
                ),
            ],
            final_observation=final_observation(
                [{"write_id": "w1", "version": 1}, {"write_id": "w2", "version": 2}]
            ),
        )
        history.final_observation["topology"]["stable"] = False
        self.assertEqual(Outcome.PASS, check_history(history).outcome)

    def test_successful_read_without_a_concrete_version_is_indeterminate(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read"),
            ],
        )
        self.assertEqual(Outcome.INDETERMINATE, check_history(history).outcome)

    def test_wfr_requires_same_key_and_read_dependency(self) -> None:
        passing = base_history(
            "WFR",
            [
                operation("read", "read", observed_version=1),
                operation(
                    "write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    depends_on_read_id="read",
                    depends_on_version=1,
                    write_base_version=1,
                ),
            ],
            final_observation=final_observation(
                [
                    {"write_id": "w1", "version": 1},
                    {"write_id": "w2", "version": 2},
                ]
            ),
        )
        violating = History(
            manifest=passing.manifest,
            operations=[
                passing.operations[0],
                operation(
                    "write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    depends_on_read_id="read",
                    depends_on_version=1,
                    write_base_version=0,
                ),
            ],
            precondition=passing.precondition,
        )
        different_key = History(
            manifest=passing.manifest,
            operations=[
                passing.operations[0],
                operation(
                    "write",
                    "write",
                    key="y",
                    intended_version=2,
                    write_id="w2",
                    depends_on_read_id="read",
                    depends_on_version=1,
                ),
            ],
            precondition=passing.precondition,
            final_observation=passing.final_observation,
        )
        self.assertEqual(Outcome.PASS, check_history(passing).outcome)
        self.assertEqual(Outcome.VIOLATION, check_history(violating).outcome)
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(different_key).outcome)

    def test_wfr_without_execution_value_is_indeterminate(self) -> None:
        history = base_history(
            "WFR",
            [
                operation("read", "read", observed_version=1),
                operation(
                    "write",
                    "write",
                    intended_version=2,
                    write_id="w2",
                    depends_on_read_id="read",
                    depends_on_version=1,
                ),
            ],
        )
        self.assertEqual(Outcome.INDETERMINATE, check_history(history).outcome)

    def test_write_timeout_is_indeterminate_and_read_error_is_unavailable(self) -> None:
        write_timeout = base_history(
            "RYW",
            [
                operation(
                    "write",
                    "write",
                    intended_version=1,
                    write_id="w1",
                    operation_status="INDETERMINATE",
                    command_started=True,
                    response_received=False,
                ),
                operation("read", "read", observed_version=0),
            ],
        )
        read_error = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read", operation_status="UNAVAILABLE"),
            ],
        )
        self.assertEqual(Outcome.INDETERMINATE, check_history(write_timeout).outcome)
        self.assertEqual(Outcome.UNAVAILABLE, check_history(read_error).outcome)

    def test_malformed_history_is_harness_error(self) -> None:
        history = History(
            manifest={"trial_id": "fixture-1", "property": "RYW"},
            operations=[operation("write", "write", intended_version=-1, write_id="w1")],
            precondition={
                "status": "SATISFIED",
                "checks": [{"name": "fixture-state", "status": "SATISFIED"}],
            },
        )
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(history).outcome)

    def test_fault_controller_error_is_harness_error(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read", operation_status="UNAVAILABLE"),
            ],
        )
        history.fault_events.append(
            {
                "event_id": "fixture-fault",
                "action": "isolate",
                "status": "ERROR",
                "error": "connection refused",
            }
        )
        result = check_history(history)
        self.assertEqual(Outcome.HARNESS_ERROR, result.outcome)
        self.assertEqual("fixture-fault", result.details["event_id"])

    def test_history_hash_round_trip(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read", observed_version=1),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            written_hash = write_history(path, history)
            self.assertEqual(written_hash, compute_history_hash(history))
            loaded = read_history(path)
        self.assertEqual(written_hash, loaded.history_hash)

    def test_history_hash_accepts_preimage_legacy_records(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read", observed_version=1),
            ],
        )
        payload = history.to_dict(include_hash=False)
        for operation_payload in payload["operations"]:
            operation_payload.pop("write_base_observed")
            operation_payload.pop("write_base_write_ids")
        payload["history_hash"] = compute_history_hash(payload)
        loaded = History.from_dict(payload)
        self.assertEqual([], validate_history(loaded))
        self.assertEqual(Outcome.PASS, check_history(loaded).outcome)

    def test_history_write_is_atomic_from_the_reader_perspective(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", intended_version=1, write_id="w1"),
                operation("read", "read", observed_version=1),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            write_history(path, history)
            self.assertTrue(path.is_file())
            self.assertEqual([], list(root.glob("*.tmp")))

    def test_precondition_miss_is_not_counted_as_database_violation(self) -> None:
        history = History(
            manifest={"trial_id": "fixture-1", "property": "RYW"},
            operations=[],
            precondition={
                "status": "PRECONDITION_MISS",
                "checks": [{
                    "name": "stale-member-remained-at-v0",
                    "status": "PRECONDITION_MISS",
                }],
            },
        )
        self.assertEqual(Outcome.PRECONDITION_MISS, check_history(history).outcome)

    def test_exception_classification_distinguishes_unsent_and_ambiguous_writes(self) -> None:
        class ServerSelectionTimeout(Exception):
            pass

        class NetworkTimeout(Exception):
            pass

        class LocalHarnessFailure(Exception):
            pass

        self.assertEqual(
            "UNAVAILABLE",
            classify_exception(ServerSelectionTimeout(), "write", command_started=False)[0],
        )
        self.assertEqual(
            "INDETERMINATE",
            classify_exception(NetworkTimeout(), "write", command_started=True)[0],
        )
        self.assertEqual(
            "HARNESS_ERROR",
            classify_exception(LocalHarnessFailure(), "write", command_started=False)[0],
        )


if __name__ == "__main__":
    unittest.main()
