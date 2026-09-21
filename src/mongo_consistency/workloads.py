"""Property schedules with explicit state construction and independent checks."""

from __future__ import annotations

import time
from typing import Any

from .faults import FaultControllerClient, FaultControllerError
from .models import OperationRecord
from .topology import TopologyError, TopologyState
from .trial import MongoTrial

ELECTION_BARRIER_SECONDS = 30.0


class ScheduleError(RuntimeError):
    """A schedule could not be executed by the experiment harness."""


class PreconditionMiss(RuntimeError):
    """The requested schedule state could not be established and verified."""


def _record_topology_precondition(trial: MongoTrial, state: TopologyState) -> str:
    runtime_metadata = getattr(trial, "runtime_metadata", {})
    plan = runtime_metadata.get("rq3_topology_plan")
    expected_primary = plan.get("initial_primary") if isinstance(plan, dict) else None
    satisfied = (
        state.stable
        and state.primary is not None
        and len(state.secondaries) == 2
        and (expected_primary is None or state.primary == expected_primary)
    )
    trial.record_precondition(
        "one-primary-two-secondary-topology",
        satisfied=satisfied,
        expected={"roles": "1 PRIMARY + 2 SECONDARY", "primary": expected_primary},
        actual={"primary": state.primary, "secondaries": list(state.secondaries)},
    )
    if not satisfied or state.primary is None:
        raise PreconditionMiss("topology oracle did not observe one primary and two secondaries")
    return state.primary


def _rq3_plan(trial: MongoTrial) -> dict[str, Any] | None:
    runtime_metadata = getattr(trial, "runtime_metadata", {})
    value = runtime_metadata.get("rq3_topology_plan")
    return value if isinstance(value, dict) else None


def _record_rq3_prestate(trial: MongoTrial, stage: str) -> None:
    if _rq3_plan(trial) is None:
        return
    trial.record_diagnostic(
        "rq3-prestate",
        {
            "stage": stage,
            "members": trial.oracle.observe_all(
                trial.database_name,
                trial.collection_name,
                trial.document_id,
            ),
        },
    )


def _freeze_election_guard(trial: MongoTrial, event_id: str) -> str | None:
    plan = _rq3_plan(trial)
    guard = plan.get("election_guard_member") if plan else None
    if not isinstance(guard, str):
        return None
    state = trial.oracle.member_state(guard)
    satisfied = state.get("reachable") is True and state.get("role") == "SECONDARY"
    trial.record_diagnostic("rq3-election-guard-before", state)
    trial.record_precondition(
        "rq3-election-guard-secondary",
        satisfied=satisfied,
        expected="SECONDARY",
        actual=state.get("role"),
    )
    if not satisfied:
        raise PreconditionMiss(f"election guard {guard} is not a reachable secondary")
    trial.oracle.freeze_member(guard, 120)
    trial.manifest.setdefault("rq3_election_guards", []).append(
        {"member": guard, "action": "freeze", "event_id": event_id}
    )
    return guard


def _unfreeze_election_guard(trial: MongoTrial, guard: str | None, event_id: str) -> None:
    if guard is None:
        return
    trial.oracle.freeze_member(guard, 0)
    state = trial.oracle.member_state(guard)
    satisfied = state.get("reachable") is True and state.get("role") == "SECONDARY"
    trial.record_diagnostic("rq3-election-guard-after", state)
    trial.record_precondition(
        "rq3-election-guard-unfrozen",
        satisfied=satisfied,
        expected="reachable SECONDARY after election",
        actual=state.get("role"),
    )
    if not satisfied:
        raise PreconditionMiss(f"election guard {guard} did not remain a secondary")
    trial.manifest.setdefault("rq3_election_guards", []).append(
        {"member": guard, "action": "unfreeze", "event_id": event_id}
    )


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
    if action == "isolate":
        trial.set_fault_state(set(members), event_id)
    try:
        if action == "isolate":
            responses = controller.isolate_many(members, event_id)
            expected_isolated = True
        elif action == "heal":
            responses = controller.heal_many(members, event_id)
            expected_isolated = False
        else:
            raise FaultControllerError(f"unknown fault action: {action}")
        health = controller.health()
        verified = all(
            health.get(member, {}).get("replication_isolated") is expected_isolated
            for member in members
        )
        if not verified:
            raise FaultControllerError(
                f"fault controller health did not confirm {action} for {members}: {health!r}"
            )
        if action == "heal":
            trial.set_fault_state(set(), None)
        event.update(
            {
                "status": "APPLIED",
                "responses": responses,
                "health": health,
                "rules_verified": True,
                "control_path_reachable": True,
                "applied_ns": time.monotonic_ns(),
                "end_ns": time.monotonic_ns(),
            }
        )
        trial.update_fault_event(event_id, event)
    except FaultControllerError as error:
        updates = {
            "status": "ERROR",
            "error": str(error),
            "end_ns": time.monotonic_ns(),
        }
        event.update(updates)
        trial.update_fault_event(event_id, updates)
        raise


def _wait_for_majority_primary(
    trial: MongoTrial,
    old_primary: str,
    event_id: str,
    expected_primary: str | None = None,
) -> str:
    start_ns = time.monotonic_ns()
    trial.update_fault_event(event_id, {"election_start_ns": start_ns})
    try:
        wait_kwargs = {
            "timeout_seconds": min(ELECTION_BARRIER_SECONDS, trial.remaining_seconds())
        }
        if expected_primary is not None:
            wait_kwargs["expected_primary"] = expected_primary
        new_primary = trial.oracle.wait_for_majority_primary(old_primary, **wait_kwargs)
    except TopologyError as error:
        trial.manifest["schedule_outcome"] = {
            "outcome": "UNAVAILABLE",
            "phase": "election",
            "error": str(error),
        }
        trial.update_fault_event(
            event_id,
            {"election_end_ns": time.monotonic_ns(), "election_error": str(error)},
        )
        raise
    trial.update_fault_event(
        event_id,
        {
            "election_end_ns": time.monotonic_ns(),
            "new_primary": new_primary,
        },
    )
    trial.record_diagnostic(
        "majority-side-election",
        {
            "old_primary": old_primary,
            "new_primary": new_primary,
            "expected_primary": expected_primary,
        },
    )
    return new_primary


def _check_member_access(trial: MongoTrial, member: str, *, expected_role: str) -> dict[str, Any]:
    state = trial.oracle.member_state(member)
    trial.record_diagnostic("direct-member-access", state)
    trial.record_precondition(
        f"client-path-{member}",
        satisfied=state.get("reachable") is True,
        expected="reachable over the client network",
        actual={"reachable": state.get("reachable"), "error": state.get("error")},
    )
    if state.get("reachable") is not True:
        raise PreconditionMiss(f"{member} is not reachable after the replication-path fault")
    role_matches = state.get("role") == expected_role
    trial.record_precondition(
        f"role-{member}",
        satisfied=role_matches,
        expected=expected_role,
        actual=state.get("role"),
    )
    if not role_matches:
        raise PreconditionMiss(f"{member} role changed to {state.get('role')!r}")
    return state


def _observe_member(
    trial: MongoTrial,
    member: str,
    *,
    expected_version: int,
    expected_write_ids: list[str] | None = None,
) -> dict[str, Any]:
    observation = trial.oracle.observe_document(
        member,
        trial.database_name,
        trial.collection_name,
        trial.document_id,
    )
    trial.record_diagnostic(f"document-state-{member}", observation)
    actual = {
        "reachable": observation.get("reachable"),
        "observed_version": observation.get("observed_version"),
        "observed_write_ids": observation.get("observed_write_ids"),
    }
    expected = {
        "reachable": True,
        "observed_version": expected_version,
    }
    if expected_write_ids is not None:
        expected["observed_write_ids"] = expected_write_ids
    satisfied = (
        actual["reachable"] is True
        and actual["observed_version"] == expected_version
        and (
            expected_write_ids is None
            or actual["observed_write_ids"] == expected_write_ids
        )
    )
    trial.record_precondition(
        f"document-state-{member}-v{expected_version}",
        satisfied=satisfied,
        expected=expected,
        actual=actual,
    )
    if not satisfied:
        raise PreconditionMiss(f"{member} did not show the required logical version")
    return observation


def _wait_for_member_state(
    trial: MongoTrial,
    member: str,
    *,
    expected_version: int,
    expected_write_ids: list[str] | None = None,
    timeout_seconds: float = ELECTION_BARRIER_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(timeout_seconds, trial.remaining_seconds())
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = trial.oracle.observe_document(
            member,
            trial.database_name,
            trial.collection_name,
            trial.document_id,
        )
        ids_match = expected_write_ids is None or last.get("observed_write_ids") == expected_write_ids
        if (
            last.get("reachable") is True
            and last.get("observed_version") == expected_version
            and ids_match
        ):
            trial.record_diagnostic(f"document-state-{member}", last)
            trial.record_precondition(
                f"document-state-{member}-v{expected_version}",
                satisfied=True,
                expected={"observed_version": expected_version, "observed_write_ids": expected_write_ids},
                actual={
                    "observed_version": last.get("observed_version"),
                    "observed_write_ids": last.get("observed_write_ids"),
                },
            )
            return last
        time.sleep(0.1)
    trial.record_diagnostic(f"document-state-timeout-{member}", last)
    trial.mark_precondition_miss(
        f"document-state-{member}-v{expected_version}",
        expected={"observed_version": expected_version, "observed_write_ids": expected_write_ids},
        actual={
            "observed_version": last.get("observed_version"),
            "observed_write_ids": last.get("observed_write_ids"),
            "reachable": last.get("reachable"),
        },
    )
    raise PreconditionMiss(f"{member} did not reach version {expected_version} before deadline")


def _record_route_if_started(
    trial: MongoTrial,
    operation: OperationRecord,
    expected_member: str,
) -> None:
    if not operation.command_started:
        return
    if not trial.verify_requested_route(operation, expected_member):
        raise PreconditionMiss(
            f"{operation.operation_id} reached {operation.actual_server_address!r}, "
            f"expected {expected_member}"
        )


def _require_controller(
    controller: FaultControllerClient | None,
    property_name: str,
) -> FaultControllerClient:
    if controller is None:
        raise FaultControllerError(f"adversarial {property_name} requires a fault controller")
    return controller


def _rw_schedule(
    trial: MongoTrial,
    *,
    adversarial: bool,
    controller: FaultControllerClient | None,
    event_id: str,
) -> None:
    trial.set_steps({"write": "write", "read": "read"})
    initial = trial.oracle.wait_for_stable(ELECTION_BARRIER_SECONDS)
    primary = _record_topology_precondition(trial, initial)
    stale, fresh = initial.secondaries
    plan = _rq3_plan(trial)
    if plan is not None:
        stale = str(plan["subject_read_member"])
        fresh = next(member for member in initial.secondaries if member != stale)
    if adversarial:
        active_controller = _require_controller(controller, "RYW")
        _fault_event(
            trial,
            active_controller,
            event_id=event_id,
            action="isolate",
            members=[stale],
        )
        _check_member_access(trial, stale, expected_role="SECONDARY")
        _observe_member(trial, stale, expected_version=0, expected_write_ids=["init"])
    write = trial.write("write", write_id="w1", intended_version=1, fault_event_id=event_id if adversarial else None)
    _record_route_if_started(trial, write, primary)
    if write.operation_status != "SUCCESS":
        return
    if adversarial:
        _wait_for_member_state(trial, fresh, expected_version=1, expected_write_ids=["init", "w1"])
        _observe_member(trial, stale, expected_version=0, expected_write_ids=["init"])
    else:
        for member in initial.secondaries:
            _wait_for_member_state(
                trial,
                member,
                expected_version=1,
                expected_write_ids=["init", "w1"],
            )
    _record_rq3_prestate(trial, "before-read")
    read = trial.read(
        "read",
        requested_member=stale,
        fault_event_id=event_id if adversarial else None,
    )
    _record_route_if_started(trial, read, stale)


def _mr_schedule(
    trial: MongoTrial,
    *,
    adversarial: bool,
    controller: FaultControllerClient | None,
    event_id: str,
) -> None:
    trial.set_steps({"first_read": "first_read", "second_read": "second_read"})
    initial = trial.oracle.wait_for_stable(ELECTION_BARRIER_SECONDS)
    primary = _record_topology_precondition(trial, initial)
    stale, fresh = initial.secondaries
    if adversarial:
        active_controller = _require_controller(controller, "MR")
        _fault_event(
            trial,
            active_controller,
            event_id=event_id,
            action="isolate",
            members=[stale],
        )
        _check_member_access(trial, stale, expected_role="SECONDARY")
        _observe_member(trial, stale, expected_version=0, expected_write_ids=["init"])
    setup = trial.setup_write(
        primary,
        write_id="prep",
        version=1,
        write_concern="majority",
    )
    if setup.get("status") != "SUCCESS":
        trial.mark_precondition_miss(
            "mr-setup-write",
            expected="version 1 acknowledged on the healthy majority",
            actual=setup,
        )
        return
    _wait_for_member_state(trial, fresh, expected_version=1, expected_write_ids=["init", "prep"])
    if adversarial:
        _observe_member(trial, stale, expected_version=0, expected_write_ids=["init"])
    else:
        for member in initial.secondaries:
            _wait_for_member_state(
                trial,
                member,
                expected_version=1,
                expected_write_ids=["init", "prep"],
            )
    first = trial.read(
        "first_read",
        requested_member=fresh,
        fault_event_id=event_id if adversarial else None,
    )
    _record_route_if_started(trial, first, fresh)
    if first.operation_status == "SUCCESS":
        second = trial.read(
            "second_read",
            requested_member=stale,
            fault_event_id=event_id if adversarial else None,
        )
        _record_route_if_started(trial, second, stale)


def _mw_schedule(
    trial: MongoTrial,
    *,
    adversarial: bool,
    controller: FaultControllerClient | None,
    event_id: str,
) -> None:
    trial.set_steps({"first_write": "first_write", "second_write": "second_write"})
    initial = trial.oracle.wait_for_stable(ELECTION_BARRIER_SECONDS)
    old_primary = _record_topology_precondition(trial, initial)
    plan = _rq3_plan(trial)
    if plan is not None:
        _record_rq3_prestate(trial, "before-first-write")
    if not adversarial:
        first = trial.write("first_write", write_id="w1", intended_version=1)
        _record_route_if_started(trial, first, old_primary)
        if first.operation_status == "SUCCESS":
            second = trial.write(
                "second_write",
                write_id="w2",
                intended_version=2,
                parent_write_id="w1",
            )
            _record_route_if_started(trial, second, old_primary)
        return

    active_controller = _require_controller(controller, "MW")
    guard = _freeze_election_guard(trial, event_id)
    _fault_event(
        trial,
        active_controller,
        event_id=event_id,
        action="isolate",
        members=[str(plan["isolation_target"]) if plan is not None else old_primary],
    )
    isolated_member = str(plan["isolation_target"]) if plan is not None else old_primary
    _check_member_access(trial, isolated_member, expected_role="PRIMARY")
    first = trial.write(
        "first_write",
        write_id="w1",
        intended_version=1,
        fault_event_id=event_id,
    )
    expected_first = str(plan["first_write_member"]) if plan is not None else old_primary
    _record_route_if_started(trial, first, expected_first)
    if first.operation_status != "SUCCESS":
        trial.manifest["schedule_outcome"] = {
            "outcome": first.operation_status,
            "phase": "isolated-old-primary-write",
            "command_started": first.command_started,
        }
    expected_new = (
        str(plan["expected_new_primary"])
        if plan is not None and plan.get("expected_new_primary") is not None
        else None
    )
    new_primary = _wait_for_majority_primary(trial, isolated_member, event_id, expected_new)
    _unfreeze_election_guard(trial, guard, event_id)
    expected_second = str(plan["second_write_member"]) if plan is not None else new_primary
    second = trial.write(
        "second_write",
        write_id="w2",
        intended_version=2,
        parent_write_id="w1",
        fault_event_id=event_id,
    )
    _record_route_if_started(trial, second, expected_second)


def _wfr_schedule(
    trial: MongoTrial,
    *,
    adversarial: bool,
    controller: FaultControllerClient | None,
    event_id: str,
) -> None:
    trial.set_steps({"read": "read", "write": "write"})
    initial = trial.oracle.wait_for_stable(ELECTION_BARRIER_SECONDS)
    old_primary = _record_topology_precondition(trial, initial)
    plan = _rq3_plan(trial)
    fresh_secondary = initial.secondaries[0]
    if adversarial:
        active_controller = _require_controller(controller, "WFR")
        guard = _freeze_election_guard(trial, event_id)
        _fault_event(
            trial,
            active_controller,
            event_id=event_id,
            action="isolate",
            members=[old_primary],
        )
        _check_member_access(trial, old_primary, expected_role="PRIMARY")
        setup = trial.setup_write(
            old_primary,
            write_id="w1",
            version=1,
            write_concern="w:1",
        )
        if plan is not None and not trial.verify_setup_route(
            setup, str(plan["first_write_member"])
        ):
            raise PreconditionMiss("M2 setup W1 did not reach its planned member")
        if setup.get("status") != "SUCCESS":
            trial.mark_precondition_miss(
                "wfr-old-branch-write",
                expected="version 1 written on the isolated old primary with w:1",
                actual=setup,
            )
            return
        _observe_member(trial, old_primary, expected_version=1, expected_write_ids=["init", "w1"])
        for member in initial.secondaries:
            _observe_member(trial, member, expected_version=0, expected_write_ids=["init"])
        _record_rq3_prestate(trial, "before-read")
        read = trial.read(
            "read",
            requested_member=(
                str(plan["subject_read_member"]) if plan is not None else old_primary
            ),
            force_primary=True,
            fault_event_id=event_id,
        )
        expected_read = (
            str(plan["subject_read_member"]) if plan is not None else old_primary
        )
        _record_route_if_started(trial, read, expected_read)
        if read.operation_status != "SUCCESS":
            return
        read_version = read.observed_version
        if read_version is None:
            trial.mark_precondition_miss(
                "wfr-concrete-read-version",
                expected="a concrete version returned by subject R1",
                actual=None,
            )
            return
        try:
            expected_new = (
                str(plan["expected_new_primary"])
                if plan is not None and plan.get("expected_new_primary") is not None
                else None
            )
            new_primary = _wait_for_majority_primary(
                trial, old_primary, event_id, expected_new
            )
        except TopologyError:
            return
        _unfreeze_election_guard(trial, guard, event_id)
    else:
        setup = trial.setup_write(
            old_primary,
            write_id="w1",
            version=1,
            write_concern="majority",
        )
        if plan is not None and not trial.verify_setup_route(setup, old_primary):
            raise PreconditionMiss("M2 setup W1 did not reach its planned member")
        if setup.get("status") != "SUCCESS":
            trial.mark_precondition_miss(
                "wfr-setup-write",
                expected="version 1 acknowledged on the stable replica set",
                actual=setup,
            )
            return
        for member in initial.secondaries:
            _wait_for_member_state(
                trial,
                member,
                expected_version=1,
                expected_write_ids=["init", "w1"],
            )
        read = trial.read("read", requested_member=fresh_secondary)
        _record_route_if_started(trial, read, fresh_secondary)
        if read.operation_status != "SUCCESS":
            return
        read_version = read.observed_version
        if read_version is None:
            trial.mark_precondition_miss(
                "wfr-concrete-read-version",
                expected="a concrete version returned by subject R1",
                actual=None,
            )
            return
        new_primary = old_primary

    write = trial.write(
        "write",
        write_id="w2",
        intended_version=2,
        depends_on_read_id="read",
        depends_on_version=read_version,
        fault_event_id=event_id if adversarial else None,
    )
    if adversarial:
        _record_route_if_started(trial, write, new_primary)
    else:
        _record_route_if_started(trial, write, old_primary)


def run_property(
    trial: MongoTrial,
    *,
    adversarial: bool,
    controller: FaultControllerClient | None,
) -> None:
    """Construct the registered property history, heal, and observe independently."""

    property_name = trial.property_name
    event_id = f"{trial.trial_id}-{property_name.lower()}-fault"
    schedules = {
        "RYW": _rw_schedule,
        "MR": _mr_schedule,
        "MW": _mw_schedule,
        "WFR": _wfr_schedule,
    }
    schedule = schedules.get(property_name)
    if schedule is None:
        trial.manifest["runner_error"] = f"unknown property: {property_name}"
        return
    if not trial.precondition_satisfied:
        return
    try:
        schedule(
            trial,
            adversarial=adversarial,
            controller=controller if adversarial else None,
            event_id=event_id,
        )
    except PreconditionMiss as error:
        trial.mark_precondition_miss(
            "registered-schedule-state",
            expected="property-specific diagnostic checks pass",
            actual=str(error),
        )
    except TopologyError as error:
        if not trial.manifest.get("schedule_outcome"):
            trial.mark_precondition_miss(
                "topology-oracle",
                expected="required direct-member topology state",
                actual=str(error),
            )
    finally:
        active_members = sorted(trial.active_fault_members)
        if active_members and controller is not None:
            heal_event_id = f"{event_id}-heal"
            try:
                _fault_event(
                    trial,
                    controller,
                    event_id=heal_event_id,
                    action="heal",
                    members=active_members,
                )
                recovery_start_ns = time.monotonic_ns()
                trial.update_fault_event(
                    heal_event_id,
                    {"recovery_start_ns": recovery_start_ns},
                )
                state = trial.oracle.wait_for_stable(ELECTION_BARRIER_SECONDS)
                recovery_end_ns = time.monotonic_ns()
                trial.update_fault_event(
                    heal_event_id,
                    {
                        "recovery_end_ns": recovery_end_ns,
                        "stable_topology": state.to_dict(),
                    },
                )
                trial.manifest["cleanup_status"] = "STABLE"
            except Exception as error:  # noqa: BLE001 - cleanup failures invalidate the history.
                trial.manifest["cleanup_status"] = "ERROR"
                trial.manifest["cleanup_error"] = str(error)
        if trial.manifest.get("cleanup_status") != "ERROR":
            trial.capture_final_observation(ELECTION_BARRIER_SECONDS)
