"""Property-specific workloads and fault schedules."""

from __future__ import annotations

import time
from typing import Any

from .faults import FaultControllerClient, FaultControllerError
from .models import OperationRecord
from .trial import MongoTrial

ELECTION_BARRIER_SECONDS = 30.0


class ScheduleError(RuntimeError):
    """A schedule could not establish its required topology state."""


def _secondary_members(trial: MongoTrial) -> tuple[str, ...]:
    trial.refresh_roles()
    members = [
        address.split(":", 1)[0]
        for address, role in trial.roles.items()
        if role and "Secondary" in role
    ]
    if len(members) >= 2:
        return tuple(sorted(members))
    return ("mongo2", "mongo3")


def _primary_member(trial: MongoTrial) -> str:
    primary = trial.primary_member()
    if primary is None:
        raise ScheduleError("no primary is visible before the schedule")
    return primary


def _members_for_stale_read(trial: MongoTrial) -> tuple[str, str]:
    primary = _primary_member(trial)
    secondaries = tuple(member for member in _secondary_members(trial) if member != primary)
    if len(secondaries) < 2:
        raise ScheduleError("two secondary members are required for the read schedules")
    return secondaries[0], secondaries[1]


def _stale_secondary_member(trial: MongoTrial) -> str:
    primary = _primary_member(trial)
    secondaries = tuple(member for member in _secondary_members(trial) if member != primary)
    if not secondaries:
        raise ScheduleError("a secondary member is required for the read schedule")
    return secondaries[0]


def _fault_event(
    trial: MongoTrial,
    controller: FaultControllerClient,
    *,
    event_id: str,
    action: str,
    members: list[str],
) -> None:
    started_ns = time.monotonic_ns()
    event: dict[str, Any] = {
        "event_id": event_id,
        "action": action,
        "members": list(members),
        "start_ns": started_ns,
        "status": "STARTED",
    }
    trial.add_fault_event(event)
    try:
        if action == "isolate":
            responses = controller.isolate_many(members, event_id)
        elif action == "heal":
            responses = controller.heal_many(members, event_id)
        else:
            raise FaultControllerError(f"unknown fault action: {action}")
    except FaultControllerError as error:
        updates = {
            "status": "ERROR",
            "error": str(error),
            "end_ns": time.monotonic_ns(),
        }
        event.update(updates)
        trial.update_fault_event(event_id, updates)
        raise
    event.update(
        {
            "status": "APPLIED",
            "responses": responses,
            "applied_ns": time.monotonic_ns(),
            "end_ns": time.monotonic_ns(),
        }
    )
    trial.update_fault_event(event_id, event)


def _wait_for_new_primary(trial: MongoTrial, old_primary: str, event_id: str) -> str:
    election_start_ns = time.monotonic_ns()
    trial.update_fault_event(
        event_id,
        {"election_start_ns": election_start_ns},
    )
    deadline = time.monotonic() + ELECTION_BARRIER_SECONDS
    while time.monotonic() < deadline:
        trial.ensure_deadline()
        trial.refresh_roles()
        primary = trial.primary_member()
        if primary is not None and primary != old_primary:
            trial.update_fault_event(
                event_id,
                {
                    "election_end_ns": time.monotonic_ns(),
                    "new_primary": primary,
                },
            )
            return primary
        time.sleep(0.25)
    raise ScheduleError(
        f"new primary did not appear within {ELECTION_BARRIER_SECONDS:.0f}s "
        f"after isolating {old_primary}"
    )


def _mark_unsupported(trial: MongoTrial, step_ids: list[str], message: str) -> None:
    trial.manifest["precondition_status"] = "UNSUPPORTED"
    trial.manifest["precondition_error"] = message
    existing = {operation.operation_id for operation in trial.operations}
    for operation_id in step_ids:
        if operation_id in existing:
            continue
        trial.operations.append(
            OperationRecord(
                operation_id=operation_id,
                kind="setup",
                key="x",
                operation_status="UNSUPPORTED",
                error_message=message,
            )
        )


def run_property(
    trial: MongoTrial,
    *,
    adversarial: bool,
    controller: FaultControllerClient | None,
) -> None:
    """Run one property-specific history inside an already-open trial."""

    property_name = trial.property_name
    event_id = f"{trial.trial_id}-{property_name.lower()}-fault"
    active_fault_members: list[str] = []
    fault_active = False
    try:
        trial.wait_for_stable_topology(ELECTION_BARRIER_SECONDS)
        if property_name == "RYW":
            trial.set_steps({"write": "write", "read": "read"})
            stale = _stale_secondary_member(trial)
            if adversarial:
                if controller is None:
                    raise FaultControllerError("adversarial RYW requires a fault controller")
                _fault_event(
                    trial,
                    controller,
                    event_id=event_id,
                    action="isolate",
                    members=[stale],
                )
                active_fault_members = [stale]
                fault_active = True
            trial.write("write", write_id="w1", version=1, fault_event_id=event_id if fault_active else None)
            trial.read(
                "read",
                requested_member=stale if adversarial else None,
                fault_event_id=event_id if fault_active else None,
            )
            return

        if property_name == "MR":
            trial.set_steps({"first_read": "first_read", "second_read": "second_read"})
            stale, fresh = _members_for_stale_read(trial)
            trial.write("precondition_write", write_id="prep", version=1)
            if adversarial:
                if controller is None:
                    raise FaultControllerError("adversarial MR requires a fault controller")
                _fault_event(
                    trial,
                    controller,
                    event_id=event_id,
                    action="isolate",
                    members=[stale],
                )
                active_fault_members = [stale]
                fault_active = True
            trial.read(
                "first_read",
                requested_member=fresh,
                fault_event_id=event_id if fault_active else None,
            )
            trial.read(
                "second_read",
                requested_member=stale,
                fault_event_id=event_id if fault_active else None,
            )
            return

        if property_name == "MW":
            trial.set_steps(
                {"first_write": "first_write", "second_write": "second_write", "observer": "observer"}
            )
            old_primary = _primary_member(trial)
            trial.write("first_write", write_id="w1", version=1)
            if adversarial:
                if controller is None:
                    raise FaultControllerError("adversarial MW requires a fault controller")
                _fault_event(
                    trial,
                    controller,
                    event_id=event_id,
                    action="isolate",
                    members=[old_primary],
                )
                active_fault_members = [old_primary]
                fault_active = True
                _wait_for_new_primary(trial, old_primary, event_id)
            trial.write(
                "second_write",
                write_id="w2",
                version=2,
                parent_write_id="w1",
                fault_event_id=event_id if fault_active else None,
            )
            trial.observe("observer", fault_event_id=event_id if fault_active else None)
            return

        if property_name == "WFR":
            trial.set_steps({"read": "read", "write": "write", "observer": "observer"})
            old_primary = _primary_member(trial)
            trial.write("precondition_write", write_id="w1", version=1)
            trial.read("read", fault_event_id=event_id if fault_active else None)
            if adversarial:
                if controller is None:
                    raise FaultControllerError("adversarial WFR requires a fault controller")
                _fault_event(
                    trial,
                    controller,
                    event_id=event_id,
                    action="isolate",
                    members=[old_primary],
                )
                active_fault_members = [old_primary]
                fault_active = True
                _wait_for_new_primary(trial, old_primary, event_id)
            read_operation = next(operation for operation in trial.operations if operation.operation_id == "read")
            trial.write(
                "write",
                write_id="w2",
                version=2,
                depends_on_read_id="read",
                depends_on_version=read_operation.observed_version,
                fault_event_id=event_id if fault_active else None,
            )
            trial.observe("observer", fault_event_id=event_id if fault_active else None)
            return

        raise ScheduleError(f"unknown property: {property_name}")
    except FaultControllerError as error:
        steps = list(trial.manifest.get("property_steps", {}).values())
        _mark_unsupported(trial, steps, str(error))
    except ScheduleError as error:
        steps = list(trial.manifest.get("property_steps", {}).values())
        _mark_unsupported(trial, steps, str(error))
    finally:
        if fault_active and controller is not None:
            heal_event_id = f"{event_id}-heal"
            try:
                _fault_event(
                    trial,
                    controller,
                    event_id=heal_event_id,
                    action="heal",
                    members=active_fault_members,
                )
                recovery_start_ns = time.monotonic_ns()
                trial.update_fault_event(
                    heal_event_id,
                    {"recovery_start_ns": recovery_start_ns},
                )
                trial.wait_for_stable_topology(ELECTION_BARRIER_SECONDS)
                recovery_end_ns = time.monotonic_ns()
                trial.update_fault_event(
                    heal_event_id,
                    {"recovery_end_ns": recovery_end_ns},
                )
                trial.manifest["cleanup_status"] = "STABLE"
            except FaultControllerError as error:
                trial.manifest["cleanup_status"] = "ERROR"
                trial.manifest["cleanup_error"] = str(error)
            except Exception as error:  # noqa: BLE001  # Cleanup must be recorded.
                trial.manifest["cleanup_status"] = "ERROR"
                trial.manifest["cleanup_error"] = str(error)
