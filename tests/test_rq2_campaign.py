from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from mongo_consistency.models import OperationRecord
from run_rq2_campaign import (
    CasePlan,
    CaseState,
    _run_after_fault,
    _run_first_operation,
)


class FakeTrial:
    def __init__(self, property_name: str) -> None:
        self.trial_id = f"rq2-fixture-{property_name.lower()}"
        self.configuration = {
            "id": "C4",
            "read_concern": "local",
            "write_concern": "majority",
            "causal_session": True,
        }
        self.precondition = {"status": "SATISFIED", "checks": []}
        self.manifest: dict[str, Any] = {}
        self.operations: list[OperationRecord] = []
        self.setup_calls: list[dict[str, Any]] = []
        self.write_calls: list[dict[str, Any]] = []

    @property
    def precondition_satisfied(self) -> bool:
        return self.precondition["status"] == "SATISFIED"

    def reset_deadline(self) -> None:
        return None

    def verify_setup_route(self, setup: dict[str, Any], expected_member: str) -> bool:
        return setup.get("actual_server_address") == f"{expected_member}:27017"

    def verify_requested_route(self, operation: OperationRecord, expected_member: str) -> bool:
        return operation.actual_server_address == f"{expected_member}:27017"

    def setup_write(
        self,
        member: str,
        *,
        write_id: str,
        version: int,
        write_concern: str,
        **_: Any,
    ) -> dict[str, Any]:
        self.setup_calls.append(
            {
                "member": member,
                "write_id": write_id,
                "version": version,
                "write_concern": write_concern,
            }
        )
        return {
            "status": "SUCCESS",
            "command_started": True,
            "actual_server_address": f"{member}:27017",
        }

    def read(
        self,
        operation_id: str,
        *,
        requested_member: str,
        force_primary: bool,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        del force_primary
        operation = OperationRecord(
            operation_id=operation_id,
            kind="read",
            key="x",
            operation_status="SUCCESS",
            command_started=True,
            response_received=True,
            session_id="subject-session",
            actual_server_address=f"{requested_member}:27017",
            observed_document_exists=True,
            observed_version=1,
            observed_versions=(0, 1),
            observed_write_ids=("init", "w1"),
            fault_event_id=fault_event_id,
        )
        self.operations.append(operation)
        return operation

    def write(
        self,
        operation_id: str,
        *,
        write_id: str,
        intended_version: int,
        parent_write_id: str | None = None,
        depends_on_read_id: str | None = None,
        depends_on_version: int | None = None,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        self.write_calls.append(
            {
                "operation_id": operation_id,
                "write_id": write_id,
                "intended_version": intended_version,
                "parent_write_id": parent_write_id,
                "depends_on_read_id": depends_on_read_id,
                "depends_on_version": depends_on_version,
            }
        )
        operation = OperationRecord(
            operation_id=operation_id,
            kind="write",
            key="x",
            operation_status="SUCCESS",
            command_started=True,
            response_received=True,
            session_id="subject-session",
            actual_server_address="mongo2:27017",
            intended_version=intended_version,
            write_id=write_id,
            parent_write_id=parent_write_id,
            depends_on_read_id=depends_on_read_id,
            depends_on_version=depends_on_version,
            write_base_observed=True,
            write_base_version=0,
            write_base_write_ids=("init",),
            fault_event_id=fault_event_id,
        )
        self.operations.append(operation)
        return operation

    def mark_precondition_miss(self, name: str, *, expected: Any, actual: Any) -> None:
        self.precondition["status"] = "PRECONDITION_MISS"
        self.precondition["checks"].append(
            {"name": name, "expected": expected, "actual": actual}
        )


def make_case(property_name: str) -> CaseState:
    plan = CasePlan(
        ordinal=1,
        topology_condition="F2",
        repetition=1,
        configuration_id="C4",
        property_name=property_name,
        signature_extension=False,
    )
    return CaseState(
        plan=plan,
        configuration={
            "id": "C4",
            "read_concern": "local",
            "write_concern": "majority",
            "causal_session": True,
        },
        trial=FakeTrial(property_name),
        initialized=True,
    )


class RQ2ScheduleTests(unittest.TestCase):
    def test_mr_seed_is_setup_outside_the_subject_history(self) -> None:
        case = make_case("MR")

        _run_first_operation(case, "mongo1", signature_deferred=False)

        assert case.trial is not None
        self.assertEqual(
            [{"member": "mongo1", "write_id": "w1", "version": 1, "write_concern": "majority"}],
            case.trial.setup_calls,
        )
        self.assertEqual([], case.trial.write_calls)
        self.assertEqual(["first_read"], [operation.operation_id for operation in case.trial.operations])
        self.assertEqual(1, case.read_version)

    def test_wfr_read_is_seeded_before_the_subject_dependency(self) -> None:
        case = make_case("WFR")

        _run_first_operation(case, "mongo1", signature_deferred=False)
        _run_after_fault(
            case,
            topology_condition="F2",
            old_primary="mongo1",
            new_primary="mongo2",
            surviving_secondary=None,
            event_id="fault-1",
        )

        assert case.trial is not None
        self.assertEqual("majority", case.trial.setup_calls[0]["write_concern"])
        self.assertEqual(["read", "write"], [operation.operation_id for operation in case.trial.operations])
        self.assertEqual(2, case.trial.write_calls[0]["intended_version"])
        self.assertEqual("read", case.trial.write_calls[0]["depends_on_read_id"])
        self.assertEqual(1, case.trial.write_calls[0]["depends_on_version"])

    def test_f3_wfr_first_read_is_deferred_until_after_partition(self) -> None:
        case = make_case("WFR")

        _run_first_operation(case, "mongo1", signature_deferred=True)

        assert case.trial is not None
        self.assertEqual([], case.trial.setup_calls)
        self.assertEqual([], case.trial.operations)


if __name__ == "__main__":
    unittest.main()
