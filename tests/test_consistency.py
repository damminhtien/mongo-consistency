from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations, load_predictions
from mongo_consistency.history import compute_history_hash, read_history, write_history
from mongo_consistency.models import History, OperationRecord, Outcome


def operation(operation_id: str, kind: str, key: str = "x", **kwargs: object) -> OperationRecord:
    return OperationRecord(operation_id=operation_id, kind=kind, key=key, **kwargs)


def base_history(property_name: str, operations: list[OperationRecord]) -> History:
    return History(
        manifest={"trial_id": "fixture-1", "property": property_name},
        operations=operations,
    )


class ConfigurationTests(unittest.TestCase):
    def test_full_factorial_matrix_and_predictions_are_loadable(self) -> None:
        configurations = load_configurations(ROOT / "configs/configurations.json")
        predictions = load_predictions(ROOT / "configs/predictions.json")
        self.assertEqual(tuple(f"C{number}" for number in range(1, 9)), tuple(configurations))
        self.assertEqual(tuple(configurations), tuple(predictions))


class CheckerTests(unittest.TestCase):
    def test_ryw_pass_and_violation(self) -> None:
        passing = base_history(
            "RYW",
            [
                operation("write", "write", version=2, write_id="w1"),
                operation("read", "read", observed_version=2),
            ],
        )
        violating = base_history(
            "RYW",
            [
                operation("write", "write", version=2, write_id="w1"),
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

    def test_mw_requires_same_key_and_checks_one_snapshot(self) -> None:
        passing = base_history(
            "MW",
            [
                operation("first_write", "write", version=1, write_id="w1"),
                operation(
                    "second_write",
                    "write",
                    version=2,
                    write_id="w2",
                    parent_write_id="w1",
                ),
                operation(
                    "observer",
                    "observer",
                    observed_updates=(
                        {"write_id": "w1", "version": 1},
                        {"write_id": "w2", "version": 2},
                    ),
                ),
            ],
        )
        violating = History(
            manifest=passing.manifest,
            operations=[
                passing.operations[0],
                passing.operations[1],
                operation(
                    "observer",
                    "observer",
                    observed_updates=({"write_id": "w2", "version": 2},),
                ),
            ],
        )
        different_key = History(
            manifest=passing.manifest,
            operations=[
                passing.operations[0],
                operation(
                    "second_write",
                    "write",
                    key="y",
                    version=2,
                    write_id="w2",
                    parent_write_id="w1",
                ),
                passing.operations[2],
            ],
        )
        self.assertEqual(Outcome.PASS, check_history(passing).outcome)
        self.assertEqual(Outcome.VIOLATION, check_history(violating).outcome)
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(different_key).outcome)

    def test_wfr_requires_same_key_and_read_dependency(self) -> None:
        passing = base_history(
            "WFR",
            [
                operation("read", "read", observed_version=1),
                operation(
                    "write",
                    "write",
                    version=2,
                    write_id="w2",
                    depends_on_read_id="read",
                    depends_on_version=1,
                ),
                operation(
                    "observer",
                    "observer",
                    observed_updates=({"write_id": "w2", "version": 2},),
                    observed_versions=(1, 2),
                ),
            ],
        )
        violating = History(
            manifest=passing.manifest,
            operations=[
                passing.operations[0],
                passing.operations[1],
                operation(
                    "observer",
                    "observer",
                    observed_updates=({"write_id": "w2", "version": 2},),
                    observed_versions=(2,),
                ),
            ],
        )
        different_key = History(
            manifest=passing.manifest,
            operations=[
                passing.operations[0],
                operation(
                    "write",
                    "write",
                    key="y",
                    version=2,
                    write_id="w2",
                    depends_on_read_id="read",
                    depends_on_version=1,
                ),
                passing.operations[2],
            ],
        )
        self.assertEqual(Outcome.PASS, check_history(passing).outcome)
        self.assertEqual(Outcome.VIOLATION, check_history(violating).outcome)
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(different_key).outcome)

    def test_write_timeout_is_indeterminate_and_read_error_is_unavailable(self) -> None:
        write_timeout = base_history(
            "RYW",
            [
                operation(
                    "write",
                    "write",
                    operation_status="TIMEOUT",
                    response_received=False,
                ),
                operation("read", "read", observed_version=0),
            ],
        )
        read_error = base_history(
            "RYW",
            [
                operation("write", "write", version=1, write_id="w1"),
                operation("read", "read", operation_status="UNAVAILABLE"),
            ],
        )
        self.assertEqual(Outcome.INDETERMINATE, check_history(write_timeout).outcome)
        self.assertEqual(Outcome.UNAVAILABLE, check_history(read_error).outcome)

    def test_malformed_history_is_harness_error(self) -> None:
        history = History(
            manifest={"trial_id": "fixture-1", "property": "RYW"},
            operations=[operation("write", "write", version=-1, write_id="w1")],
        )
        self.assertEqual(Outcome.HARNESS_ERROR, check_history(history).outcome)

    def test_fault_controller_error_is_harness_error(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", version=1, write_id="w1"),
                operation("read", "read", operation_status="UNSUPPORTED"),
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
                operation("write", "write", version=1, write_id="w1"),
                operation("read", "read", observed_version=1),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            written_hash = write_history(path, history)
            self.assertEqual(written_hash, compute_history_hash(history))
            loaded = read_history(path)
        self.assertEqual(written_hash, loaded.history_hash)
        self.assertEqual(Outcome.PASS, check_history(loaded).outcome)

    def test_history_write_is_atomic_from_the_reader_perspective(self) -> None:
        history = base_history(
            "RYW",
            [
                operation("write", "write", version=1, write_id="w1"),
                operation("read", "read", observed_version=1),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            write_history(path, history)
            self.assertTrue(path.is_file())
            self.assertEqual([], list(root.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
