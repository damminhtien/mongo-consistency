"""Offline checkers for the four client-centric consistency properties."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .history import validate_history
from .models import CheckerResult, History, OperationRecord, Outcome


def _result(outcome: Outcome, reason: str, **details: Any) -> CheckerResult:
    return CheckerResult(outcome=outcome, reason=reason, details=details)


def _operations(history: History) -> dict[str, OperationRecord]:
    return {operation.operation_id: operation for operation in history.operations}


def _step_ids(history: History, defaults: Iterable[str]) -> list[str]:
    steps = history.manifest.get("property_steps", {})
    return [str(steps.get(name, name)) for name in defaults]


def _preflight(
    history: History,
    property_name: str,
    step_names: Iterable[str],
) -> tuple[dict[str, OperationRecord] | None, CheckerResult | None]:
    errors = validate_history(history)
    if errors:
        return None, _result(Outcome.HARNESS_ERROR, "history schema is invalid", errors=errors)
    if history.manifest.get("cleanup_status") == "ERROR":
        return None, _result(
            Outcome.HARNESS_ERROR,
            "fault cleanup failed and the cluster state is not trusted",
            error=history.manifest.get("cleanup_error"),
        )
    if history.manifest.get("runner_error"):
        return None, _result(
            Outcome.HARNESS_ERROR,
            "runner recorded an execution error",
            error=history.manifest["runner_error"],
        )
    failed_fault_event = next(
        (event for event in history.fault_events if event.get("status") == "ERROR"),
        None,
    )
    if failed_fault_event is not None:
        return None, _result(
            Outcome.HARNESS_ERROR,
            "fault controller failed to apply or verify the requested topology change",
            event_id=failed_fault_event.get("event_id"),
            action=failed_fault_event.get("action"),
            error=failed_fault_event.get("error"),
        )
    if history.manifest.get("property") != property_name:
        return None, _result(
            Outcome.HARNESS_ERROR,
            "history property does not match checker",
            expected=property_name,
            actual=history.manifest.get("property"),
        )
    precondition_status = history.precondition.get("status")
    if precondition_status == "PRECONDITION_MISS":
        return None, _result(
            Outcome.PRECONDITION_MISS,
            "the required schedule state was not established",
            checks=history.precondition.get("checks", []),
        )
    if precondition_status != "SATISFIED":
        return None, _result(
            Outcome.HARNESS_ERROR,
            "precondition status is missing or invalid",
            status=precondition_status,
        )

    schedule_outcome = history.manifest.get("schedule_outcome")
    if isinstance(schedule_outcome, dict):
        outcome_name = schedule_outcome.get("outcome")
        if outcome_name in {Outcome.UNAVAILABLE.value, Outcome.INDETERMINATE.value}:
            outcome = Outcome(outcome_name)
            return None, _result(
                outcome,
                "the registered schedule could not complete a required database phase",
                phase=schedule_outcome.get("phase"),
                error=schedule_outcome.get("error"),
            )
        if outcome_name == Outcome.HARNESS_ERROR.value:
            return None, _result(
                Outcome.HARNESS_ERROR,
                "the registered schedule recorded a harness failure",
                phase=schedule_outcome.get("phase"),
                error=schedule_outcome.get("error"),
            )

    operations = _operations(history)
    names = list(step_names)
    ids = _step_ids(history, names)
    selected: dict[str, OperationRecord] = {}
    for name, operation_id in zip(names, ids, strict=True):
        operation = operations.get(operation_id)
        if operation is None:
            prior_failure = next(
                (
                    item
                    for item in selected.values()
                    if item.operation_status in {"UNAVAILABLE", "INDETERMINATE", "HARNESS_ERROR"}
                ),
                None,
            )
            if prior_failure is not None:
                outcome = Outcome(prior_failure.operation_status)
                return None, _result(
                    outcome,
                    "a required later subject operation was not issued after an earlier operation failed",
                    operation_id=prior_failure.operation_id,
                    step=name,
                    error=prior_failure.error_message,
                )
            return None, _result(
                Outcome.HARNESS_ERROR,
                "required subject operation is missing",
                operation_id=operation_id,
                step=name,
            )
        selected[name] = operation
    operation_positions = {
        operation.operation_id: index
        for index, operation in enumerate(history.operations)
    }
    expected_order = [operation.operation_id for operation in selected.values()]
    actual_order = [
        operation.operation_id
        for operation in sorted(
            selected.values(),
            key=lambda operation: operation_positions[operation.operation_id],
        )
    ]
    if expected_order != actual_order:
        return None, _result(
            Outcome.HARNESS_ERROR,
            "subject operations are out of order",
            expected_order=expected_order,
            actual_order=actual_order,
        )
    for name, operation in selected.items():
        status = operation.operation_status
        if status == "HARNESS_ERROR":
            return None, _result(
                Outcome.HARNESS_ERROR,
                "runner reported a harness error",
                operation_id=operation.operation_id,
                step=name,
                error=operation.error_message,
            )
        if status == "INDETERMINATE":
            return None, _result(
                Outcome.INDETERMINATE,
                "the operation reached MongoDB but its effect is unresolved",
                operation_id=operation.operation_id,
                step=name,
                command_started=operation.command_started,
                error=operation.error_message,
            )
        if status == "UNAVAILABLE":
            return None, _result(
                Outcome.UNAVAILABLE,
                "the required subject operation did not complete",
                operation_id=operation.operation_id,
                step=name,
                command_started=operation.command_started,
                error=operation.error_message,
            )
        if status != "SUCCESS":
            return None, _result(
                Outcome.HARNESS_ERROR,
                "subject operation status is invalid",
                operation_id=operation.operation_id,
                status=status,
            )
    return selected, None


def _same_key(operations: Iterable[OperationRecord]) -> CheckerResult | None:
    keys = {operation.key for operation in operations}
    if len(keys) != 1:
        return _result(
            Outcome.HARNESS_ERROR,
            "property history does not use one logical key",
            keys=sorted(keys),
        )
    return None


def _same_process(operations: Iterable[OperationRecord]) -> CheckerResult | None:
    """Require the recorded operations to belong to one subject session."""

    session_ids = {operation.session_id for operation in operations}
    if len(session_ids) > 1:
        return _result(
            Outcome.HARNESS_ERROR,
            "property history uses more than one subject session",
            session_ids=sorted(str(session_id) for session_id in session_ids),
        )
    if None in session_ids:
        return _result(
            Outcome.INDETERMINATE,
            "same-process evidence is missing because the subject session was not recorded",
        )
    return None


def _read_version(operation: OperationRecord) -> int | None:
    if operation.observed_version is not None:
        return operation.observed_version
    return max(operation.observed_versions) if operation.observed_versions else None


def _require_version(
    operation: OperationRecord,
    step: str,
) -> tuple[int | None, CheckerResult | None]:
    version = _read_version(operation)
    if version is None:
        return None, _result(
            Outcome.INDETERMINATE,
            "successful read has no observable logical version",
            operation_id=operation.operation_id,
            step=step,
        )
    return version, None


def _final_updates(history: History) -> tuple[list[dict[str, Any]] | None, CheckerResult | None]:
    observation = history.final_observation
    if not isinstance(observation, dict):
        return None, _result(
            Outcome.INDETERMINATE,
            "independent post-heal observation is missing",
        )
    if observation.get("converged") is not True:
        return None, _result(
            Outcome.INDETERMINATE,
            "replicas did not produce a converged final observation",
            error=observation.get("error"),
        )
    topology = observation.get("topology")
    expected_members = {"mongo1", "mongo2", "mongo3"}
    if not isinstance(topology, dict) or topology.get("stable") is not True:
        return None, _result(
            Outcome.INDETERMINATE,
            "replica-set topology was not independently verified stable after healing",
            topology=topology,
        )
    primary = topology.get("primary")
    secondaries = topology.get("secondaries")
    if (
        primary not in expected_members
        or not isinstance(secondaries, list)
        or set(secondaries) != expected_members - {primary}
    ):
        return None, _result(
            Outcome.INDETERMINATE,
            "final topology snapshot does not show one primary and two secondaries",
            topology=topology,
        )
    members = observation.get("members")
    if not isinstance(members, dict) or set(members) != expected_members:
        return None, _result(
            Outcome.INDETERMINATE,
            "final observation does not contain all three members",
            members=members,
        )
    updates: list[list[dict[str, Any]]] = []
    for member, state in members.items():
        if not isinstance(state, dict) or state.get("reachable") is not True:
            return None, _result(
                Outcome.INDETERMINATE,
                "a member could not be read by the independent observer",
                member=member,
                state=state,
            )
        if state.get("exists") is not True or state.get("observation_valid") is not True:
            return None, _result(
                Outcome.INDETERMINATE,
                "independent observer did not obtain a valid logical document",
                member=member,
                state=state,
            )
        observed = state.get("updates")
        if not isinstance(observed, list) or any(not isinstance(item, dict) for item in observed):
            return None, _result(
                Outcome.INDETERMINATE,
                "final member snapshot has no valid update list",
                member=member,
            )
        updates.append(observed)
    if any(value != updates[0] for value in updates[1:]):
        return None, _result(
            Outcome.INDETERMINATE,
            "direct member snapshots disagree despite the convergence flag",
        )
    return updates[0], None


def _visible_ids(updates: list[dict[str, Any]]) -> set[str]:
    return {
        str(update["write_id"])
        for update in updates
        if isinstance(update.get("write_id"), str)
    }


def _visible_versions(updates: list[dict[str, Any]]) -> set[int]:
    return {
        int(update["version"])
        for update in updates
        if isinstance(update.get("version"), int)
        and not isinstance(update.get("version"), bool)
    }


def check_ryw(history: History) -> CheckerResult:
    """Check whether a subject read is at least as new as its preceding write."""

    selected, error = _preflight(history, "RYW", ("write", "read"))
    if error:
        return error
    assert selected is not None
    if same_key := _same_key(selected.values()):
        return same_key
    if same_process := _same_process(selected.values()):
        return same_process
    write, read = selected["write"], selected["read"]
    if write.intended_version is None:
        return _result(Outcome.HARNESS_ERROR, "write has no intended logical version")
    read_version, error = _require_version(read, "read")
    if error:
        return error
    assert read_version is not None
    if read_version < write.intended_version:
        return _result(
            Outcome.VIOLATION,
            "read returned a version older than the preceding subject write",
            write_version=write.intended_version,
            read_version=read_version,
        )
    return _result(
        Outcome.PASS,
        "read returned the written version or a later version",
        write_version=write.intended_version,
        read_version=read_version,
    )


def check_mr(history: History) -> CheckerResult:
    """Check that successive subject reads do not move to an older version."""

    selected, error = _preflight(history, "MR", ("first_read", "second_read"))
    if error:
        return error
    assert selected is not None
    if same_key := _same_key(selected.values()):
        return same_key
    if same_process := _same_process(selected.values()):
        return same_process
    first, second = selected["first_read"], selected["second_read"]
    first_version, error = _require_version(first, "first_read")
    if error:
        return error
    second_version, error = _require_version(second, "second_read")
    if error:
        return error
    assert first_version is not None and second_version is not None
    if second_version < first_version:
        return _result(
            Outcome.VIOLATION,
            "second read returned a version older than the first read",
            first_version=first_version,
            second_version=second_version,
        )
    return _result(
        Outcome.PASS,
        "successive reads did not move to an older version",
        first_version=first_version,
        second_version=second_version,
    )


def check_mw(history: History) -> CheckerResult:
    """Check monotonic writes from the state preceding the successive write."""

    selected, error = _preflight(history, "MW", ("first_write", "second_write"))
    if error:
        return error
    assert selected is not None
    if same_key := _same_key(selected.values()):
        return same_key
    if same_process := _same_process(selected.values()):
        return same_process
    first, second = selected["first_write"], selected["second_write"]
    if not first.write_id:
        return _result(
            Outcome.HARNESS_ERROR,
            "preceding write has no stable write identifier",
        )
    if not second.write_id:
        return _result(
            Outcome.HARNESS_ERROR,
            "successive write has no stable write identifier",
        )
    if second.parent_write_id != first.write_id:
        return _result(
            Outcome.HARNESS_ERROR,
            "successive write does not identify the preceding write",
            expected_parent_write_id=first.write_id,
            actual_parent_write_id=second.parent_write_id,
        )
    if not second.response_received:
        return _result(
            Outcome.INDETERMINATE,
            "successive write has no definitive client response",
        )
    if not second.write_base_observed:
        return _result(
            Outcome.INDETERMINATE,
            "state immediately preceding the successive write was not recorded",
            predecessor_write_id=first.write_id,
        )
    if first.write_id not in second.write_base_write_ids:
        return _result(
            Outcome.VIOLATION,
            "successive write took place before the preceding write was present",
            predecessor_write_id=first.write_id,
            write_base_write_ids=list(second.write_base_write_ids),
        )
    return _result(
        Outcome.PASS,
        "preceding write was present in the state used by the successive write",
        predecessor_write_id=first.write_id,
        write_base_write_ids=list(second.write_base_write_ids),
    )


def check_wfr(history: History) -> CheckerResult:
    """Check the Lecture 3 value-order definition of writes-follow-reads."""

    selected, error = _preflight(history, "WFR", ("read", "write"))
    if error:
        return error
    assert selected is not None
    if same_key := _same_key(selected.values()):
        return same_key
    if same_process := _same_process(selected.values()):
        return same_process
    read, write = selected["read"], selected["write"]
    read_version, error = _require_version(read, "read")
    if error:
        return error
    assert read_version is not None
    if write.depends_on_read_id != read.operation_id:
        return _result(
            Outcome.HARNESS_ERROR,
            "dependent write does not name the preceding subject read",
            expected_read_id=read.operation_id,
            actual_read_id=write.depends_on_read_id,
        )
    if write.depends_on_version != read_version:
        return _result(
            Outcome.HARNESS_ERROR,
            "dependent write does not name the version returned by the read",
            expected_version=read_version,
            actual_version=write.depends_on_version,
        )
    if not write.write_base_observed:
        return _result(
            Outcome.INDETERMINATE,
            "state immediately preceding the dependent write was not recorded",
            read_version=read_version,
        )
    if write.write_base_version is None:
        return _result(
            Outcome.INDETERMINATE,
            "the value observed by the dependent write was not recorded",
            read_version=read_version,
        )
    if write.write_base_version < read_version:
        return _result(
            Outcome.VIOLATION,
            "dependent write took place on an older value than the preceding read",
            read_version=read_version,
            write_base_version=write.write_base_version,
        )
    return _result(
        Outcome.PASS,
        "dependent write took place on the read value or a more recent value",
        read_version=read_version,
        write_base_version=write.write_base_version,
    )


CHECKERS = {"RYW": check_ryw, "MR": check_mr, "MW": check_mw, "WFR": check_wfr}


def check_history(history: History, property_name: str | None = None) -> CheckerResult:
    """Run the checker selected by the manifest or explicit property name."""

    selected_property = property_name or str(history.manifest.get("property", ""))
    checker = CHECKERS.get(selected_property)
    if checker is None:
        return _result(
            Outcome.HARNESS_ERROR,
            "no checker is registered for the property",
            property=selected_property,
        )
    return checker(history)
