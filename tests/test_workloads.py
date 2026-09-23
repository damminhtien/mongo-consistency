from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mongo_consistency.checkers import check_history
from mongo_consistency.models import History, OperationRecord, Outcome
from mongo_consistency.topology import TopologyState
from mongo_consistency.workloads import run_property


MEMBERS = ("mongo1", "mongo2", "mongo3")


class FakeOracle:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.primary = "mongo1"
        self.roles = {
            "mongo1": "PRIMARY",
            "mongo2": "SECONDARY",
            "mongo3": "SECONDARY",
        }
        self.sync_sources = {
            "mongo1": None,
            "mongo2": "mongo1:27017",
            "mongo3": "mongo2:27017",
        }
        self.fail_sync_source_wait = False
        self.documents = {
            member: [
                {
                    "write_id": "init",
                    "version": 0,
                    "parent_write_id": None,
                    "depends_on_read_id": None,
                    "depends_on_version": None,
                }
            ]
            for member in MEMBERS
        }

    def wait_for_stable(self, _timeout: float) -> TopologyState:
        secondaries = tuple(sorted(member for member, role in self.roles.items() if role == "SECONDARY"))
        return TopologyState(
            primary=self.primary,
            secondaries=secondaries,
            members={
                member: {
                    "member": member,
                    "me": f"{member}:27017",
                    "reachable": True,
                    "role": role,
                }
                for member, role in self.roles.items()
            },
            stable=True,
            observed_at_ns=1,
        )

    def member_state(self, member: str) -> dict[str, Any]:
        self.timeline.append(f"diagnostic:hello:{member}")
        return {"member": member, "reachable": True, "role": self.roles[member]}

    def sync_source(self, member: str) -> str | None:
        self.timeline.append(f"diagnostic:sync-source:{member}")
        return self.sync_sources[member]

    def sync_from(self, member: str, source_address: str) -> dict[str, Any]:
        previous = self.sync_sources[member]
        self.sync_sources[member] = source_address
        self.timeline.append(f"replication:sync-from:{member}:{source_address}")
        return {
            "ok": 1,
            "prevSyncTarget": previous,
            "syncFromRequested": source_address,
        }

    def wait_for_sync_source(
        self,
        member: str,
        source_address: str,
        *,
        timeout_seconds: float,
    ) -> str:
        del timeout_seconds
        source = self.sync_sources[member]
        if self.fail_sync_source_wait or source != source_address:
            raise RuntimeError(f"{member} still syncs from {source!r}")
        self.timeline.append(f"replication:sync-source-ready:{member}:{source_address}")
        return source

    def wait_for_majority_primary(self, isolated_member: str, timeout_seconds: float) -> str:
        del timeout_seconds
        candidate = next(member for member in MEMBERS if member != isolated_member and member != self.primary)
        other = next(member for member in MEMBERS if member not in {isolated_member, candidate})
        self.primary = candidate
        self.roles = {
            isolated_member: "PRIMARY",
            candidate: "PRIMARY",
            other: "SECONDARY",
        }
        self.timeline.append(f"election:{candidate}")
        return candidate

    def observe_document(
        self,
        member: str,
        _database: str,
        _collection: str,
        _document_id: str,
    ) -> dict[str, Any]:
        updates = [dict(update) for update in self.documents[member]]
        self.timeline.append(f"diagnostic:document:{member}:{self._version(updates)}")
        versions = [update["version"] for update in updates]
        return {
            "member": member,
            "reachable": True,
            "exists": True,
            "observation_valid": True,
            "updates": updates,
            "observed_versions": versions,
            "observed_write_ids": [update["write_id"] for update in updates],
            "observed_version": max(versions),
        }

    def wait_for_document_convergence(
        self,
        _database: str,
        _collection: str,
        _document_id: str,
        _timeout: float,
    ) -> dict[str, Any]:
        final_updates = [dict(update) for update in self.documents[self.primary]]
        for member in MEMBERS:
            self.documents[member] = [dict(update) for update in final_updates]
        members = {member: self.observe_document(member, "", "", "") for member in MEMBERS}
        self.timeline.append("diagnostic:converged")
        return {"members": members, "converged": True}

    @staticmethod
    def _version(updates: list[dict[str, Any]]) -> int:
        return max(update["version"] for update in updates)


class FakeController:
    def __init__(self, timeline: list[str], oracle: FakeOracle) -> None:
        self.timeline = timeline
        self.oracle = oracle
        self.isolated: set[str] = set()

    def isolate_many(self, members: list[str], event_id: str) -> list[dict[str, Any]]:
        self.isolated.update(members)
        self.timeline.append(f"fault:isolate:{','.join(members)}")
        return [{"member": member, "event_id": event_id} for member in members]

    def heal_many(self, members: list[str], event_id: str) -> list[dict[str, Any]]:
        self.isolated.difference_update(members)
        self.oracle.roles = {
            member: "PRIMARY" if member == self.oracle.primary else "SECONDARY"
            for member in MEMBERS
        }
        self.timeline.append(f"fault:heal:{','.join(members)}")
        return [{"member": member, "event_id": event_id} for member in members]

    def health(self) -> dict[str, dict[str, Any]]:
        return {
            member: {"replication_isolated": member in self.isolated}
            for member in MEMBERS
        }


class FakeTrial:
    def __init__(self, property_name: str, *, no_wfr_read_version: bool = False) -> None:
        self.trial_id = f"fixture-{property_name.lower()}"
        self.property_name = property_name
        self.configuration = {
            "id": "C1",
            "read_concern": "local",
            "write_concern": "w:1",
            "causal_session": False,
        }
        self.timeline: list[str] = []
        self.oracle = FakeOracle(self.timeline)
        self.controller = FakeController(self.timeline, self.oracle)
        self.no_wfr_read_version = no_wfr_read_version
        self.precondition = {"status": "SATISFIED", "checks": []}
        self.operations: list[OperationRecord] = []
        self.clock_ns = 0
        self.diagnostics: list[dict[str, Any]] = []
        self.fault_events: list[dict[str, Any]] = []
        self.final_observation: dict[str, Any] | None = None
        self.active_fault_members: set[str] = set()
        self.active_fault_event_id: str | None = None
        self.manifest: dict[str, Any] = {
            "trial_id": self.trial_id,
            "property": property_name,
            "cleanup_status": "NOT_RUN",
        }
        self.session_id = "session-fixture"
        self.database_name = "db"
        self.collection_name = "logical"
        self.document_id = "x"

    @property
    def precondition_satisfied(self) -> bool:
        return self.precondition["status"] == "SATISFIED"

    def remaining_seconds(self) -> float:
        return 60.0

    def set_steps(self, steps: dict[str, str]) -> None:
        self.manifest["property_steps"] = dict(steps)

    def record_diagnostic(self, name: str, payload: dict[str, Any]) -> None:
        self.diagnostics.append({"name": name, "data": dict(payload)})

    def record_precondition(self, name: str, *, satisfied: bool, expected: Any, actual: Any) -> bool:
        status = "SATISFIED" if satisfied else "PRECONDITION_MISS"
        self.precondition["checks"].append(
            {"name": name, "status": status, "expected": expected, "actual": actual}
        )
        if not satisfied:
            self.precondition["status"] = "PRECONDITION_MISS"
        return satisfied

    def mark_precondition_miss(self, name: str, *, expected: Any, actual: Any) -> None:
        self.record_precondition(name, satisfied=False, expected=expected, actual=actual)

    def set_fault_state(self, members: set[str], event_id: str | None) -> None:
        self.active_fault_members = set(members)
        self.active_fault_event_id = event_id if members else None

    def add_fault_event(self, event: dict[str, Any]) -> None:
        self.fault_events.append(dict(event))

    def update_fault_event(self, event_id: str, updates: dict[str, Any]) -> None:
        for event in self.fault_events:
            if event["event_id"] == event_id:
                event.update(updates)
                return

    def _operation(
        self,
        operation_id: str,
        kind: str,
        *,
        member: str,
        write_id: str | None = None,
        intended_version: int | None = None,
        parent_write_id: str | None = None,
        depends_on_read_id: str | None = None,
        depends_on_version: int | None = None,
        observed_updates: list[dict[str, Any]] | None = None,
    ) -> OperationRecord:
        updates = observed_updates or []
        versions = [update["version"] for update in updates]
        start_ns = self.clock_ns
        self.clock_ns += 1
        end_ns = self.clock_ns
        self.clock_ns += 1
        return OperationRecord(
            operation_id=operation_id,
            kind=kind,
            key="x",
            trial_id=self.trial_id,
            property=self.property_name,
            session_id=self.session_id,
            causal_session=False,
            requested_member=member if kind == "read" else "primary",
            actual_server_address=f"{member}:27017",
            actual_role=self.oracle.roles[member],
            command_started=True,
            response_received=True,
            start_ns=start_ns,
            end_ns=end_ns,
            intended_version=intended_version,
            write_id=write_id,
            parent_write_id=parent_write_id,
            depends_on_read_id=depends_on_read_id,
            depends_on_version=depends_on_version,
            observed_document_exists=True if kind == "read" else None,
            observed_updates=tuple(dict(update) for update in updates),
            observed_versions=tuple(versions),
            observed_write_ids=tuple(update["write_id"] for update in updates),
            observed_version=max(versions) if versions else None,
        )

    def verify_requested_route(self, operation: OperationRecord, expected_member: str) -> bool:
        actual_member = operation.actual_server_address.split(":", 1)[0]
        return self.record_precondition(
            f"route-{operation.operation_id}",
            satisfied=actual_member == expected_member,
            expected=expected_member,
            actual=actual_member,
        )

    def setup_write(
        self,
        member: str,
        *,
        write_id: str,
        version: int,
        write_concern: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self.timeline.append(f"setup:{write_id}:{member}:{write_concern}")
        update = {
            "write_id": write_id,
            "version": version,
            "parent_write_id": None,
            "depends_on_read_id": None,
            "depends_on_version": None,
        }
        if replace:
            self.oracle.documents[member] = [update]
        else:
            recipients = [target for target in MEMBERS if target not in self.active_fault_members]
            if member in self.active_fault_members:
                recipients = [member]
            for target in recipients:
                self.oracle.documents[target].append(dict(update))
        return {"status": "SUCCESS", "member": member}

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
        member = self.oracle.primary
        self.timeline.append(f"subject:write:{write_id}:{member}")
        update = {
            "write_id": write_id,
            "version": intended_version,
            "parent_write_id": parent_write_id,
            "depends_on_read_id": depends_on_read_id,
            "depends_on_version": depends_on_version,
        }
        recipients = [target for target in MEMBERS if target not in self.active_fault_members]
        if member in self.active_fault_members:
            recipients = [member]
        current_versions = [
            int(item["version"])
            for item in self.oracle.documents[member]
            if isinstance(item.get("version"), int)
        ]
        current_write_ids = [
            item["write_id"]
            for item in self.oracle.documents[member]
            if isinstance(item.get("write_id"), str) and item["write_id"]
        ]
        for target in recipients:
            self.oracle.documents[target].append(dict(update))
        operation = self._operation(
            operation_id,
            "write",
            member=member,
            write_id=write_id,
            intended_version=intended_version,
            parent_write_id=parent_write_id,
            depends_on_read_id=depends_on_read_id,
            depends_on_version=depends_on_version,
        )
        operation.write_base_version = max(current_versions) if current_versions else None
        operation.write_base_write_ids = tuple(current_write_ids)
        operation.write_base_observed = True
        operation.fault_event_id = fault_event_id
        self.operations.append(operation)
        return operation

    def read(
        self,
        operation_id: str,
        *,
        requested_member: str | None = None,
        force_primary: bool = False,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        member = self.oracle.primary if force_primary else requested_member
        assert member is not None
        self.timeline.append(f"subject:read:{operation_id}:{member}")
        updates = self.oracle.documents[member]
        if self.no_wfr_read_version and operation_id == "read":
            updates = []
        operation = self._operation(operation_id, "read", member=member, observed_updates=updates)
        if not updates:
            operation.observed_version = None
            operation.observed_versions = ()
            operation.observed_write_ids = ()
        operation.fault_event_id = fault_event_id
        self.operations.append(operation)
        return operation

    def capture_final_observation(self, _timeout: float) -> dict[str, Any]:
        self.final_observation = self.oracle.wait_for_document_convergence("", "", "", 0)
        self.final_observation["topology"] = self.oracle.wait_for_stable(0).to_dict()
        return self.final_observation

    def history(self) -> History:
        return History(
            manifest=dict(self.manifest),
            precondition=dict(self.precondition),
            operations=list(self.operations),
            diagnostics=list(self.diagnostics),
            final_observation=self.final_observation,
            fault_events=list(self.fault_events),
        )


class WorkloadScheduleTests(unittest.TestCase):
    def _run(self, property_name: str, *, no_read_version: bool = False) -> FakeTrial:
        trial = FakeTrial(property_name, no_wfr_read_version=no_read_version)
        run_property(trial, adversarial=True, controller=trial.controller)
        return trial

    def test_ryw_verifies_stale_v0_before_subject_write_and_observes_violation(self) -> None:
        trial = self._run("RYW")

        isolate = trial.timeline.index("fault:isolate:mongo2")
        before_write = trial.timeline.index("diagnostic:document:mongo2:0")
        subject_write = trial.timeline.index("subject:write:w1:mongo1")
        subject_read = trial.timeline.index("subject:read:read:mongo2")
        self.assertLess(isolate, before_write)
        self.assertLess(before_write, subject_write)
        self.assertLess(subject_write, subject_read)
        self.assertEqual(Outcome.VIOLATION, check_history(trial.history()).outcome)

    def test_mr_pins_fresh_replica_to_primary_before_setup(self) -> None:
        trial = self._run("MR")

        isolate = trial.timeline.index("fault:isolate:mongo2")
        sync_from = trial.timeline.index("replication:sync-from:mongo3:mongo1:27017")
        setup = trial.timeline.index("setup:prep:mongo1:majority")
        first_read = trial.timeline.index("subject:read:first_read:mongo3")
        second_read = trial.timeline.index("subject:read:second_read:mongo2")
        self.assertLess(isolate, setup)
        self.assertLess(isolate, sync_from)
        self.assertLess(sync_from, setup)
        self.assertLess(setup, first_read)
        self.assertLess(first_read, second_read)
        self.assertEqual(2, len(trial.operations))
        self.assertEqual(Outcome.VIOLATION, check_history(trial.history()).outcome)

    def test_mr_stops_when_fresh_replication_source_cannot_be_verified(self) -> None:
        trial = FakeTrial("MR")
        trial.oracle.fail_sync_source_wait = True

        run_property(trial, adversarial=True, controller=trial.controller)

        self.assertEqual("PRECONDITION_MISS", trial.precondition["status"])
        failed_checks = [
            check["name"]
            for check in trial.precondition["checks"]
            if check["status"] == "PRECONDITION_MISS"
        ]
        self.assertEqual(
            ["mr-fresh-sync-source-primary"],
            failed_checks,
        )
        self.assertFalse(any(item.startswith("setup:prep:") for item in trial.timeline))
        self.assertEqual([], trial.operations)

    def test_mw_routes_dependent_writes_to_old_then_new_primary(self) -> None:
        trial = self._run("MW")

        first = next(operation for operation in trial.operations if operation.write_id == "w1")
        second = next(operation for operation in trial.operations if operation.write_id == "w2")
        self.assertEqual("mongo1:27017", first.actual_server_address)
        self.assertEqual("mongo2:27017", second.actual_server_address)
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual("w1", second.parent_write_id)
        self.assertEqual(("init",), second.write_base_write_ids)
        self.assertTrue(second.write_base_observed)
        self.assertLess(
            trial.timeline.index("subject:write:w1:mongo1"),
            trial.timeline.index("election:mongo2"),
        )
        self.assertLess(
            trial.timeline.index("election:mongo2"),
            trial.timeline.index("subject:write:w2:mongo2"),
        )
        self.assertEqual(Outcome.VIOLATION, check_history(trial.history()).outcome)

    def test_wfr_requires_a_concrete_read_version_before_dependent_write(self) -> None:
        trial = self._run("WFR")

        read = next(operation for operation in trial.operations if operation.kind == "read")
        write = next(operation for operation in trial.operations if operation.kind == "write")
        self.assertEqual("mongo1:27017", read.actual_server_address)
        self.assertEqual("mongo2:27017", write.actual_server_address)
        self.assertEqual(read.operation_id, write.depends_on_read_id)
        self.assertEqual(1, write.depends_on_version)
        self.assertEqual(Outcome.VIOLATION, check_history(trial.history()).outcome)

        missing = self._run("WFR", no_read_version=True)
        self.assertEqual(["read"], [operation.operation_id for operation in missing.operations])
        self.assertEqual("PRECONDITION_MISS", missing.precondition["status"])


if __name__ == "__main__":
    unittest.main()
