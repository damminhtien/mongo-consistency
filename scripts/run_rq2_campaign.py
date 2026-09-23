"""Run the grouped RQ2 fault campaign inside the restricted runner."""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_campaign import (
    _require_frozen_provenance,
    _write_json_atomic,
    campaign_runtime_metadata,
    error_history,
    schedule_id_for,
    trial_id_for,
)

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations, load_json
from mongo_consistency.faults import HostFaultCoordinatorClient
from mongo_consistency.history import read_history, write_history
from mongo_consistency.models import History, OperationRecord
from mongo_consistency.topology import TopologyError, TopologyOracle
from mongo_consistency.trial import TIMEOUT_POLICY, MongoTrial

ROOT = Path(__file__).resolve().parents[1]
PROPERTIES = ("RYW", "MR", "MW", "WFR")
CONFIGURATIONS = ("C1", "C3", "C4", "C6")
FAULTS = ("F1", "F2", "F3")
SIGNATURE_CELLS = (("C1", "RYW"), ("C6", "RYW"), ("C1", "MW"), ("C6", "MW"))
CORE_REPETITIONS = 8
SIGNATURE_REPETITIONS = 20
EXPECTED_HISTORY_COUNT = 432
EXPECTED_EPISODE_COUNT = 36
UNAVAILABLE = "UNAVAILABLE"

PROPERTY_STEPS = {
    "RYW": {"write": "write", "read": "read"},
    "MR": {"first_read": "first_read", "second_read": "second_read"},
    "MW": {"first_write": "first_write", "second_write": "second_write"},
    "WFR": {"read": "read", "write": "write"},
}


@dataclass(frozen=True)
class CasePlan:
    ordinal: int
    topology_condition: str
    repetition: int
    configuration_id: str
    property_name: str
    signature_extension: bool

    @property
    def trial_id(self) -> str:
        return trial_id_for(
            "rq2", self.ordinal, self.configuration_id, self.property_name
        )


@dataclass(frozen=True)
class EpisodePlan:
    episode_id: str
    topology_condition: str
    repetition: int
    signature_extension: bool
    cases: tuple[CasePlan, ...]


@dataclass
class CaseState:
    plan: CasePlan
    configuration: dict[str, Any]
    trial: MongoTrial | None = None
    initialized: bool = False
    first_operation: OperationRecord | None = None
    read_version: int | None = None
    route_verified: bool = True
    wfr_election_guard: str | None = None
    runner_error: str | None = None
    final_observation: dict[str, Any] | None = None


def plan_episodes() -> list[EpisodePlan]:
    """Build the frozen 384-history core and 48-history F3 extension."""

    episodes: list[EpisodePlan] = []
    ordinal = 1
    for repetition in range(1, CORE_REPETITIONS + 1):
        for condition in FAULTS:
            cases = []
            for configuration_id in CONFIGURATIONS:
                for property_name in PROPERTIES:
                    cases.append(
                        CasePlan(
                            ordinal=ordinal,
                            topology_condition=condition,
                            repetition=repetition,
                            configuration_id=configuration_id,
                            property_name=property_name,
                            signature_extension=False,
                        )
                    )
                    ordinal += 1
            episodes.append(
                EpisodePlan(
                    episode_id=f"rq2-{condition.lower()}-r{repetition:02d}",
                    topology_condition=condition,
                    repetition=repetition,
                    signature_extension=False,
                    cases=tuple(cases),
                )
            )

    for repetition in range(CORE_REPETITIONS + 1, SIGNATURE_REPETITIONS + 1):
        cases = []
        for configuration_id, property_name in SIGNATURE_CELLS:
            cases.append(
                CasePlan(
                    ordinal=ordinal,
                    topology_condition="F3",
                    repetition=repetition,
                    configuration_id=configuration_id,
                    property_name=property_name,
                    signature_extension=True,
                )
            )
            ordinal += 1
        episodes.append(
            EpisodePlan(
                episode_id=f"rq2-f3-r{repetition:02d}",
                topology_condition="F3",
                repetition=repetition,
                signature_extension=True,
                cases=tuple(cases),
            )
        )

    history_count = sum(len(episode.cases) for episode in episodes)
    if history_count != EXPECTED_HISTORY_COUNT or len(episodes) != EXPECTED_EPISODE_COUNT:
        raise ValueError("RQ2 plan does not match the registered 432 histories in 36 episodes")
    return episodes


def _new_case(
    plan: CasePlan,
    configuration: dict[str, Any],
    *,
    seed_uris: tuple[str, ...],
    runtime_metadata: dict[str, Any],
    episode: EpisodePlan,
) -> CaseState:
    metadata = {key: value for key, value in runtime_metadata.items() if key != "seed_base"}
    try:
        trial = MongoTrial(
            seed_uris=seed_uris,
            configuration=configuration,
            trial_id=plan.trial_id,
            property_name=plan.property_name,
            schedule_id=schedule_id_for(plan.property_name),
            seed=int(runtime_metadata["seed_base"]) + plan.ordinal,
            runtime_metadata=metadata,
            subtrial_deadline_seconds=TIMEOUT_POLICY["subtrial_ms"] / 1000,
        )
    except Exception as error:  # noqa: BLE001 - construction failure still has a case record.
        return CaseState(
            plan=plan,
            configuration=configuration,
            runner_error=str(error),
        )
    trial.set_campaign("rq2")
    trial.set_adversarial(True)
    trial.set_steps(PROPERTY_STEPS[plan.property_name])
    trial.manifest.update(
        {
            "campaign_ordinal": plan.ordinal,
            "topology_condition": episode.topology_condition,
            "fault_episode_id": episode.episode_id,
            "fault_repetition": episode.repetition,
            "signature_extension": episode.signature_extension,
            "fault_event_id": f"{episode.episode_id}-fault",
        }
    )
    case = CaseState(plan=plan, configuration=configuration, trial=trial)
    try:
        trial.__enter__()
        case.initialized = trial.initialize()
    except Exception as error:  # noqa: BLE001 - retain one history for every planned case.
        case.runner_error = str(error)
        trial.manifest["runner_error"] = case.runner_error
    return case


def _case_ready(case: CaseState) -> bool:
    return (
        case.trial is not None
        and case.initialized
        and case.runner_error is None
        and case.trial.precondition_satisfied
    )


def _record_route(case: CaseState, operation: OperationRecord, member: str) -> None:
    if operation.command_started and case.trial is not None:
        case.route_verified = case.trial.verify_requested_route(operation, member) and case.route_verified


def _freeze_wfr_election_guard(case: CaseState, member: str, event_id: str) -> None:
    trial = case.trial
    if trial is None or not _case_ready(case):
        return
    try:
        state = trial.oracle.member_state(member)
        satisfied = state.get("reachable") is True and state.get("role") == "SECONDARY"
        trial.record_diagnostic("rq2-wfr-election-guard-before", state)
        trial.record_precondition(
            "rq2-wfr-election-guard-secondary",
            satisfied=satisfied,
            expected="reachable SECONDARY",
            actual=state.get("role"),
        )
        if not satisfied:
            return
        trial.oracle.freeze_member(member, 120)
        trial.manifest.setdefault("rq2_wfr_election_guards", []).append(
            {"member": member, "action": "freeze", "event_id": event_id}
        )
        case.wfr_election_guard = member
    except Exception as error:  # noqa: BLE001 - guard failure prevents this schedule state.
        trial.mark_precondition_miss(
            "rq2-wfr-election-guard",
            expected={"member": member, "action": "freeze"},
            actual={"error": str(error)},
        )


def _unfreeze_wfr_election_guard(case: CaseState, event_id: str) -> None:
    trial = case.trial
    member = case.wfr_election_guard
    if trial is None or member is None:
        return
    case.wfr_election_guard = None
    try:
        trial.oracle.freeze_member(member, 0)
        state = trial.oracle.member_state(member)
        satisfied = state.get("reachable") is True and state.get("role") == "SECONDARY"
        trial.record_diagnostic("rq2-wfr-election-guard-after", state)
        trial.record_precondition(
            "rq2-wfr-election-guard-unfrozen",
            satisfied=satisfied,
            expected="reachable SECONDARY",
            actual=state.get("role"),
        )
        trial.manifest.setdefault("rq2_wfr_election_guards", []).append(
            {"member": member, "action": "unfreeze", "event_id": event_id}
        )
    except Exception as error:  # noqa: BLE001 - cleanup failure remains explicit in the history.
        trial.mark_precondition_miss(
            "rq2-wfr-election-guard-unfrozen",
            expected={"member": member, "action": "unfreeze"},
            actual={"error": str(error)},
        )


def _run_wfr_first_operation(
    case: CaseState,
    member: str,
    *,
    write_concern: str,
    fault_event_id: str | None = None,
    majority_members: tuple[str, ...] = (),
) -> None:
    trial = case.trial
    if trial is None or not _case_ready(case):
        return
    trial.reset_deadline()
    setup = trial.setup_write(
        member,
        write_id="w1",
        version=1,
        write_concern=write_concern,
    )
    setup_route_verified = trial.verify_setup_route(setup, member)
    if setup.get("status") != "SUCCESS" or not setup_route_verified:
        trial.mark_precondition_miss(
            "wfr-setup-write",
            expected={"status": "SUCCESS", "member": member, "write_concern": write_concern},
            actual=setup,
        )
        return
    if majority_members:
        try:
            branch_states = {
                branch_member: trial.oracle.observe_document(
                    branch_member,
                    trial.database_name,
                    trial.collection_name,
                    trial.document_id,
                )
                for branch_member in (member, *majority_members)
            }
        except Exception as error:  # noqa: BLE001 - failed branch evidence is a precondition miss.
            trial.mark_precondition_miss(
                "wfr-branch-state",
                expected="isolated primary at v1 and majority-side members at v0",
                actual={"error": str(error)},
            )
            return
        trial.record_diagnostic("wfr-branch-state", branch_states)
        isolated_state = branch_states[member]
        majority_states = [branch_states[branch_member] for branch_member in majority_members]
        branch_satisfied = (
            isolated_state.get("observed_versions") == [0, 1]
            and isolated_state.get("observed_write_ids") == ["init", "w1"]
            and all(
                state.get("observed_versions") == [0]
                and state.get("observed_write_ids") == ["init"]
                for state in majority_states
            )
        )
        if not trial.record_precondition(
            "wfr-branch-state",
            satisfied=branch_satisfied,
            expected={
                "isolated_member": member,
                "isolated_state": {"observed_versions": [0, 1], "observed_write_ids": ["init", "w1"]},
                "majority_members": list(majority_members),
                "majority_state": {"observed_versions": [0], "observed_write_ids": ["init"]},
            },
            actual=branch_states,
        ):
            return
    operation = trial.read(
        "read",
        requested_member=member,
        force_primary=True,
        fault_event_id=fault_event_id,
    )
    case.read_version = operation.observed_version
    _record_route(case, operation, member)
    if (
        operation.operation_status != "SUCCESS"
        or operation.observed_version != 1
        or "w1" not in operation.observed_write_ids
    ):
        trial.mark_precondition_miss(
            "wfr-first-read-sees-seed-write",
            expected={"version": 1, "write_id": "w1", "member": member},
            actual={
                "status": operation.operation_status,
                "version": operation.observed_version,
                "write_ids": list(operation.observed_write_ids),
                "member": operation.actual_server_address,
            },
        )
    case.first_operation = operation


def _run_first_operation(
    case: CaseState,
    primary: str,
    *,
    signature_deferred: bool,
    wfr_deferred: bool = False,
) -> None:
    trial = case.trial
    if trial is None or not _case_ready(case):
        return
    trial.reset_deadline()
    property_name = case.plan.property_name
    if signature_deferred and property_name in {"RYW", "MW", "WFR"}:
        return
    if wfr_deferred and property_name == "WFR":
        return
    try:
        if property_name == "RYW":
            operation = trial.write("write", write_id="w1", intended_version=1)
        elif property_name == "MR":
            setup = trial.setup_write(
                primary,
                write_id="w1",
                version=1,
                write_concern="majority",
            )
            setup_route_verified = trial.verify_setup_route(setup, primary)
            if setup.get("status") != "SUCCESS" or not setup_route_verified:
                trial.mark_precondition_miss(
                    "mr-seed-write-acknowledged",
                    expected={
                        "status": "SUCCESS",
                        "member": primary,
                        "write_concern": "majority",
                    },
                    actual=setup,
                )
                return
            operation = trial.read(
                "first_read", requested_member=primary, force_primary=True
            )
            case.read_version = operation.observed_version
            _record_route(case, operation, primary)
            if (
                operation.operation_status != "SUCCESS"
                or operation.observed_version != 1
                or "w1" not in operation.observed_write_ids
            ):
                trial.mark_precondition_miss(
                    "mr-first-read-sees-seed-write",
                    expected={"version": 1, "write_id": "w1"},
                    actual={
                        "status": operation.operation_status,
                        "version": operation.observed_version,
                        "write_ids": list(operation.observed_write_ids),
                    },
                )
            case.first_operation = operation
            return
        elif property_name == "MW":
            operation = trial.write(
                "first_write", write_id="w1", intended_version=1
            )
        else:
            _run_wfr_first_operation(
                case,
                primary,
                write_concern="majority",
            )
            return
        case.first_operation = operation
        _record_route(case, operation, primary)
    except Exception as error:  # noqa: BLE001 - isolate schedule errors to their history.
        case.runner_error = str(error)
        trial.manifest["runner_error"] = case.runner_error


def _run_signature_write(case: CaseState, old_primary: str, event_id: str) -> None:
    trial = case.trial
    if trial is None or not _case_ready(case):
        return
    trial.reset_deadline()
    try:
        if case.plan.property_name == "RYW":
            operation = trial.write(
                "write", write_id="w1", intended_version=1, fault_event_id=event_id
            )
        elif case.plan.property_name == "MW":
            operation = trial.write(
                "first_write",
                write_id="w1",
                intended_version=1,
                fault_event_id=event_id,
            )
        else:
            return
        case.first_operation = operation
        _record_route(case, operation, old_primary)
    except Exception as error:  # noqa: BLE001 - retain the other signature cases.
        case.runner_error = str(error)
        trial.manifest["runner_error"] = case.runner_error


def _has_successful_first(case: CaseState) -> bool:
    return (
        _case_ready(case)
        and case.route_verified
        and case.first_operation is not None
        and case.first_operation.operation_status == "SUCCESS"
    )


def _run_after_fault(
    case: CaseState,
    *,
    topology_condition: str,
    old_primary: str,
    new_primary: str,
    surviving_secondary: str | None,
    event_id: str,
) -> None:
    trial = case.trial
    if trial is None or not _has_successful_first(case):
        return
    trial.reset_deadline()
    property_name = case.plan.property_name
    try:
        if property_name == "RYW":
            operation = trial.read(
                "read",
                requested_member=new_primary,
                force_primary=True,
                fault_event_id=event_id,
            )
            _record_route(case, operation, new_primary)
        elif property_name == "MR":
            if topology_condition == "F3":
                member = old_primary
                operation = trial.read(
                    "second_read", requested_member=member, fault_event_id=event_id
                )
            elif topology_condition == "F1" and surviving_secondary is not None:
                member = surviving_secondary
                operation = trial.read(
                    "second_read", requested_member=member, fault_event_id=event_id
                )
            else:
                member = new_primary
                operation = trial.read(
                    "second_read",
                    requested_member=member,
                    force_primary=True,
                    fault_event_id=event_id,
                )
            _record_route(case, operation, member)
        elif property_name == "MW":
            operation = trial.write(
                "second_write",
                write_id="w2",
                intended_version=2,
                parent_write_id="w1",
                fault_event_id=event_id,
            )
            _record_route(case, operation, new_primary)
        else:
            if case.read_version is None:
                trial.mark_precondition_miss(
                    "wfr-concrete-read-version",
                    expected="R1 returned a concrete version before the fault",
                    actual=case.read_version,
                )
                return
            operation = trial.write(
                "write",
                write_id="w2",
                intended_version=2,
                depends_on_read_id="read",
                depends_on_version=case.read_version,
                fault_event_id=event_id,
            )
            _record_route(case, operation, new_primary)
    except Exception as error:  # noqa: BLE001 - retain later cases in the episode.
        case.runner_error = str(error)
        trial.manifest["runner_error"] = case.runner_error


def _wait_for_member_unreachable(
    oracle: TopologyOracle,
    member: str,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = oracle.member_state(member)
        if last_state.get("reachable") is False:
            return last_state
        time.sleep(0.2)
    raise TopologyError(f"{member} remained reachable after the registered crash: {last_state}")


def _history_for_case(case: CaseState, runtime_metadata: dict[str, Any], event: dict[str, Any] | None) -> History:
    plan = case.plan
    if case.trial is None:
        error = RuntimeError(case.runner_error or "trial client could not be created")
        history = error_history(
            trial_id=plan.trial_id,
            campaign="rq2",
            configuration=case.configuration,
            property_name=plan.property_name,
            schedule_id=schedule_id_for(plan.property_name),
            seed=int(runtime_metadata["seed_base"]) + plan.ordinal,
            error=error,
            runtime_metadata={key: value for key, value in runtime_metadata.items() if key != "seed_base"},
        )
    else:
        if case.runner_error:
            case.trial.manifest["runner_error"] = case.runner_error
        try:
            history = case.trial.history()
        except Exception as error:  # noqa: BLE001 - build an explicit record for capture errors.
            case.runner_error = str(error)
            history = error_history(
                trial_id=plan.trial_id,
                campaign="rq2",
                configuration=case.configuration,
                property_name=plan.property_name,
                schedule_id=schedule_id_for(plan.property_name),
                seed=int(runtime_metadata["seed_base"]) + plan.ordinal,
                error=error,
                runtime_metadata={key: value for key, value in runtime_metadata.items() if key != "seed_base"},
            )
        finally:
            case.trial.close()
    history.manifest.update(
        {
            "campaign_ordinal": plan.ordinal,
            "adversarial": True,
            "topology_condition": plan.topology_condition,
            "fault_episode_id": f"rq2-{plan.topology_condition.lower()}-r{plan.repetition:02d}",
            "fault_repetition": plan.repetition,
            "fault_event_id": f"rq2-{plan.topology_condition.lower()}-r{plan.repetition:02d}-fault",
            "signature_extension": plan.signature_extension,
        }
    )
    history.fault_events = [dict(event)] if event is not None else []
    return history


def _record_for_history(
    history: History,
    path: Path,
    plan: CasePlan,
    *,
    seed_base: int,
) -> dict[str, Any]:
    result = check_history(history)
    return {
        "trial_id": plan.trial_id,
        "ordinal": plan.ordinal,
        "campaign": "rq2",
        "configuration_id": plan.configuration_id,
        "property": plan.property_name,
        "adversarial": True,
        "seed": seed_base + plan.ordinal,
        "path": path.as_posix(),
        "history_hash": history.history_hash,
        "outcome": result.outcome.value,
        "precondition_status": history.precondition.get("status"),
        "runner_commit": history.manifest.get("runner_commit"),
        "runner_error": history.manifest.get("runner_error"),
        "topology_condition": plan.topology_condition,
        "fault_episode_id": f"rq2-{plan.topology_condition.lower()}-r{plan.repetition:02d}",
        "repetition": plan.repetition,
        "signature_extension": plan.signature_extension,
    }


def run_episode(
    episode: EpisodePlan,
    *,
    configurations: dict[str, dict[str, Any]],
    seed_uris: tuple[str, ...],
    runtime_metadata: dict[str, Any],
    coordinator: HostFaultCoordinatorClient,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases = [
        _new_case(
            plan,
            configurations[plan.configuration_id],
            seed_uris=seed_uris,
            runtime_metadata=runtime_metadata,
            episode=episode,
        )
        for plan in episode.cases
    ]
    event_id = f"{episode.episode_id}-fault"
    event: dict[str, Any] | None = None
    apply_attempted = False
    group_error: str | None = None
    primary: str | None = None
    faulted_member: str | None = None
    surviving_secondary: str | None = None
    new_primary: str | None = None
    lead = next((case.trial for case in cases if case.trial is not None), None)

    try:
        if lead is None:
            raise TopologyError("no trial client could be initialized for the episode")
        before = lead.wait_for_stable_topology(TIMEOUT_POLICY["election_barrier_ms"] / 1000)
        if not before.stable or before.primary is None:
            raise TopologyError(f"episode requires one primary and two secondaries: {before}")
        primary = before.primary
        if episode.topology_condition == "F1":
            target_index = (episode.repetition - 1) % len(before.secondaries)
            faulted_member = before.secondaries[target_index]
            surviving_secondary = next(
                member for member in before.secondaries if member != faulted_member
            )
        else:
            faulted_member = primary
        for case in cases:
            if case.trial is not None:
                case.trial.manifest.update(
                    {"initial_primary": primary, "faulted_member": faulted_member}
                )
                case.trial.record_diagnostic(
                    "rq2-episode-initial-topology",
                    {"episode_id": episode.episode_id, **before.to_dict()},
                )
                if episode.topology_condition == "F3" and case.plan.property_name == "WFR":
                    _freeze_wfr_election_guard(case, before.secondaries[0], event_id)
            _run_first_operation(
                case,
                primary,
                signature_deferred=(
                    episode.topology_condition == "F3"
                    and (case.plan.configuration_id, case.plan.property_name) in SIGNATURE_CELLS
                ),
                wfr_deferred=episode.topology_condition == "F3",
            )

        action = "isolate" if episode.topology_condition == "F3" else "stop"
        event = {
            "event_id": event_id,
            "episode_id": episode.episode_id,
            "topology_condition": episode.topology_condition,
            "action": action,
            "members": [faulted_member],
            "start_ns": time.monotonic_ns(),
            "status": "STARTED",
            "initial_topology": before.to_dict(),
        }
        apply_attempted = True
        applied_response = coordinator.apply(episode.topology_condition, faulted_member, event_id)
        event.update(
            {
                "status": "APPLIED",
                # Keep event intervals in the runner's monotonic clock domain.
                "applied_ns": time.monotonic_ns(),
                "coordinator_apply": applied_response,
            }
        )
        for case in cases:
            if case.trial is not None:
                case.trial.set_fault_state({faulted_member}, event_id)
                case.trial.record_precondition(
                    "rq2-fault-applied",
                    satisfied=True,
                    expected={"condition": episode.topology_condition, "member": faulted_member},
                    actual={"condition": episode.topology_condition, "member": faulted_member},
                )

        if episode.topology_condition == "F1":
            fault_observation = _wait_for_member_unreachable(lead.oracle, faulted_member)
            active_primary = lead.oracle.member_state(primary)
            active_secondary = lead.oracle.member_state(surviving_secondary or "")
            fault_verified = (
                active_primary.get("role") == "PRIMARY"
                and active_secondary.get("role") == "SECONDARY"
            )
            event["fault_observation"] = {
                "target": fault_observation,
                "primary": active_primary,
                "surviving_secondary": active_secondary,
            }
            event["fault_verified"] = fault_verified
            if not fault_verified:
                event.update(
                    {
                        "status": "ERROR",
                        "error": "secondary crash did not preserve the expected primary and surviving secondary",
                    }
                )
                for case in cases:
                    if case.trial is not None:
                        case.trial.mark_precondition_miss(
                            "rq2-secondary-crash-topology",
                            expected="target unreachable; original primary and other secondary remain available",
                            actual=event["fault_observation"],
                        )
        elif episode.topology_condition == "F3":
            signature_cases = [
                case
                for case in cases
                if (case.plan.configuration_id, case.plan.property_name) in SIGNATURE_CELLS
            ]
            if signature_cases:
                with ThreadPoolExecutor(max_workers=len(signature_cases)) as executor:
                    futures = [
                        executor.submit(_run_signature_write, case, primary, event_id)
                        for case in signature_cases
                    ]
                    for future in futures:
                        future.result()
            for case in cases:
                if case.plan.property_name == "WFR":
                    try:
                        _run_wfr_first_operation(
                            case,
                            primary,
                            write_concern="w:1",
                            fault_event_id=event_id,
                            majority_members=tuple(before.secondaries),
                        )
                    finally:
                        _unfreeze_wfr_election_guard(case, event_id)
            event["election_start_ns"] = event["applied_ns"]
            try:
                new_primary = lead.oracle.wait_for_majority_primary(
                    primary,
                    timeout_seconds=TIMEOUT_POLICY["election_barrier_ms"] / 1000,
                )
                event.update(
                    {
                        "election_end_ns": time.monotonic_ns(),
                        "new_primary": new_primary,
                    }
                )
            except TopologyError as error:
                event.update(
                    {
                        "election_end_ns": time.monotonic_ns(),
                        "election_error": str(error),
                    }
                )
                for case in cases:
                    if case.trial is not None:
                        case.trial.manifest["schedule_outcome"] = {
                            "outcome": UNAVAILABLE,
                            "phase": "majority-side-election",
                            "error": str(error),
                        }
        else:
            event["election_start_ns"] = event["applied_ns"]
            try:
                new_primary = lead.oracle.wait_for_majority_primary(
                    primary,
                    timeout_seconds=TIMEOUT_POLICY["election_barrier_ms"] / 1000,
                )
                event.update(
                    {
                        "election_end_ns": time.monotonic_ns(),
                        "new_primary": new_primary,
                    }
                )
            except TopologyError as error:
                event.update(
                    {
                        "election_end_ns": time.monotonic_ns(),
                        "election_error": str(error),
                    }
                )
                for case in cases:
                    if case.trial is not None:
                        case.trial.manifest["schedule_outcome"] = {
                            "outcome": UNAVAILABLE,
                            "phase": "primary-election",
                            "error": str(error),
                        }

        if episode.topology_condition == "F1":
            new_primary = primary
        if new_primary is not None:
            for case in cases:
                _run_after_fault(
                    case,
                    topology_condition=episode.topology_condition,
                    old_primary=primary,
                    new_primary=new_primary,
                    surviving_secondary=surviving_secondary,
                    event_id=event_id,
                )
    except Exception as error:  # noqa: BLE001 - episode failures are retained in all histories.
        group_error = str(error)
        if event is not None:
            event.update({"status": "ERROR", "error": group_error})
        for case in cases:
            if case.trial is not None:
                case.trial.manifest["runner_error"] = group_error
            else:
                case.runner_error = group_error
    finally:
        for case in cases:
            _unfreeze_wfr_election_guard(case, event_id)
        if apply_attempted and faulted_member is not None:
            recovery_requested_ns = time.monotonic_ns()
            try:
                recovery_response = coordinator.recover(
                    episode.topology_condition, faulted_member, event_id
                )
                if event is not None:
                    event.update(
                        {
                            "recovery_action": "heal" if episode.topology_condition == "F3" else "start",
                            "recovery_requested_ns": recovery_requested_ns,
                            "recovery_start_ns": recovery_requested_ns,
                            "recovery_details": recovery_response,
                        }
                    )
                for case in cases:
                    if case.trial is not None:
                        case.trial.set_fault_state(set(), None)
                        case.trial.reset_deadline()
                recovery_oracle = lead.oracle if lead is not None else TopologyOracle()
                try:
                    stable = recovery_oracle.wait_for_stable(
                        TIMEOUT_POLICY["election_barrier_ms"] / 1000
                    )
                    if event is not None:
                        event["stable_topology"] = stable.to_dict()
                except TopologyError as error:
                    stable = None
                    if event is not None:
                        event["recovery_topology_error"] = str(error)
                convergence = []
                for case in cases:
                    if case.trial is None or not case.initialized:
                        continue
                    try:
                        case.trial.reset_deadline()
                        case.final_observation = case.trial.capture_final_observation(
                            TIMEOUT_POLICY["election_barrier_ms"] / 1000
                        )
                        convergence.append(case.final_observation.get("converged") is True)
                    except Exception as error:  # noqa: BLE001 - observer failures remain data.
                        convergence.append(False)
                        case.trial.manifest["final_observation_error"] = str(error)
                all_converged = stable is not None and bool(convergence) and all(convergence)
                if event is not None:
                    event.update(
                        {
                            "recovery_end_ns": time.monotonic_ns(),
                            "recovery_status": "CONVERGED" if all_converged else "INDETERMINATE",
                            "recovery_documents_expected": len(convergence),
                            "recovery_documents_converged": sum(convergence),
                            "end_ns": time.monotonic_ns(),
                        }
                    )
            except Exception as error:  # noqa: BLE001 - the host coordinator retries cleanup.
                if event is not None:
                    event.update(
                        {
                            "status": "ERROR",
                            "recovery_status": "ERROR",
                            "recovery_error": str(error),
                            "recovery_end_ns": time.monotonic_ns(),
                            "end_ns": time.monotonic_ns(),
                        }
                    )
                for case in cases:
                    if case.trial is not None:
                        case.trial.manifest["cleanup_status"] = "ERROR"
                        case.trial.manifest["cleanup_error"] = str(error)
        elif lead is None:
            pass

    histories: list[History] = []
    for case in cases:
        history = _history_for_case(case, runtime_metadata, event)
        histories.append(history)
    episode_record = {
        "episode_id": episode.episode_id,
        "topology_condition": episode.topology_condition,
        "repetition": episode.repetition,
        "signature_extension": episode.signature_extension,
        "history_count": len(histories),
        "trial_ids": [history.manifest["trial_id"] for history in histories],
        "faulted_member": faulted_member,
        "initial_primary": primary,
        "new_primary": new_primary,
        "fault_status": event.get("status") if event else "NOT_APPLIED",
        "recovery_status": event.get("recovery_status") if event else "NOT_ATTEMPTED",
        "event": dict(event) if event is not None else None,
        "error": group_error,
    }
    return histories, episode_record


def _record_from_history(path: Path, plan: CasePlan, seed_base: int) -> dict[str, Any]:
    history = read_history(path)
    manifest = history.manifest
    expected = {
        "trial_id": plan.trial_id,
        "campaign_id": "rq2",
        "campaign_ordinal": plan.ordinal,
        "configuration_id": plan.configuration_id,
        "property": plan.property_name,
        "adversarial": True,
        "seed": seed_base + plan.ordinal,
        "topology_condition": plan.topology_condition,
        "fault_episode_id": f"rq2-{plan.topology_condition.lower()}-r{plan.repetition:02d}",
        "fault_repetition": plan.repetition,
        "signature_extension": plan.signature_extension,
    }
    mismatches = [
        f"{field}={manifest.get(field)!r}, expected {value!r}"
        for field, value in expected.items()
        if manifest.get(field) != value
    ]
    if mismatches:
        raise ValueError(f"cannot resume {path}: " + "; ".join(mismatches))
    checked = check_history(history)
    return {
        "trial_id": plan.trial_id,
        "ordinal": plan.ordinal,
        "campaign": "rq2",
        "configuration_id": plan.configuration_id,
        "property": plan.property_name,
        "adversarial": True,
        "seed": seed_base + plan.ordinal,
        "path": path.as_posix(),
        "history_hash": history.history_hash,
        "outcome": checked.outcome.value,
        "precondition_status": history.precondition.get("status"),
        "runner_commit": manifest.get("runner_commit"),
        "runner_error": manifest.get("runner_error"),
        "topology_condition": plan.topology_condition,
        "fault_episode_id": expected["fault_episode_id"],
        "repetition": plan.repetition,
        "signature_extension": plan.signature_extension,
    }


def _manifest(
    *,
    status: str,
    runtime_metadata: dict[str, Any],
    seed_base: int,
    episodes: list[EpisodePlan],
    completed_records: dict[int, dict[str, Any]],
    completed_episodes: dict[str, dict[str, Any]],
    started_ns: int,
    resumed: bool,
    finished_ns: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    planned_ordinals = list(range(1, EXPECTED_HISTORY_COUNT + 1))
    records = [completed_records[key] for key in sorted(completed_records)]
    next_ordinal = next(
        (ordinal for ordinal in planned_ordinals if ordinal not in completed_records),
        None,
    )
    payload: dict[str, Any] = {
        "schema_version": "campaign-run.v1",
        "campaign": "rq2",
        "status": status,
        "seed_base": seed_base,
        "expected_case_count": EXPECTED_HISTORY_COUNT,
        "planned_ordinals": planned_ordinals,
        "case_count": len(records),
        "completed_case_count": len(records),
        "next_ordinal": next_ordinal,
        "started_ns": started_ns,
        "last_updated_ns": time.monotonic_ns(),
        "software_versions": runtime_metadata["software_versions"],
        "image_digest": runtime_metadata["image_digest"],
        "prediction_commit": runtime_metadata.get("prediction_commit"),
        "prediction_manifest_hash": runtime_metadata.get("prediction_manifest_hash"),
        "protocol_commit": runtime_metadata.get("protocol_commit"),
        "protocol_hash": runtime_metadata.get("protocol_hash"),
        "runner_commit": runtime_metadata.get("runner_commit"),
        "runner_dirty": runtime_metadata.get("runner_dirty"),
        "runner_commits": sorted(
            {str(record["runner_commit"]) for record in records if record.get("runner_commit")}
        ),
        "resumed": resumed,
        "fault_episode_count": EXPECTED_EPISODE_COUNT,
        "completed_fault_episode_count": len(completed_episodes),
        "planned_episode_ids": [episode.episode_id for episode in episodes],
        "episodes": [completed_episodes[key] for key in (episode.episode_id for episode in episodes) if key in completed_episodes],
        "records": records,
    }
    if finished_ns is not None:
        payload["finished_ns"] = finished_ns
    if error is not None:
        payload["error"] = error
    return payload


def _episode_is_valid(record: dict[str, Any]) -> bool:
    """Return whether an episode applied, then recovered from, its registered fault."""

    event = record.get("event")
    if (
        record.get("error") is not None
        or record.get("fault_status") != "APPLIED"
        or record.get("recovery_status") != "CONVERGED"
        or not isinstance(event, dict)
        or event.get("status") != "APPLIED"
        or event.get("recovery_status") != "CONVERGED"
    ):
        return False

    apply_result = event.get("coordinator_apply")
    recovery_result = event.get("recovery_details")
    if (
        not isinstance(apply_result, dict)
        or apply_result.get("ok") is not True
        or apply_result.get("action") != "apply"
        or not isinstance(recovery_result, dict)
        or recovery_result.get("ok") is not True
        or recovery_result.get("action") != "recover"
    ):
        return False

    if record.get("topology_condition") == "F1" and event.get("fault_verified") is not True:
        return False
    if record.get("topology_condition") == "F3":
        details = apply_result.get("details")
        controller = details.get("controller") if isinstance(details, dict) else None
        if (
            not isinstance(controller, dict)
            or controller.get("verified") is not True
            or controller.get("replication_isolated") is not True
        ):
            return False
    return True


def _load_completed_episodes(
    campaign_directory: Path,
    episodes: list[EpisodePlan],
    *,
    seed_base: int,
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    records: dict[int, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    episode_root = campaign_directory / "episodes"
    if not episode_root.exists():
        return records, completed
    plan_by_id = {episode.episode_id: episode for episode in episodes}
    for directory in sorted(path for path in episode_root.iterdir() if path.is_dir()):
        episode = plan_by_id.get(directory.name)
        if episode is None:
            raise ValueError(f"unexpected RQ2 episode directory: {directory}")
        paths = sorted(directory.glob("*.json"))
        expected_names = {f"{case.trial_id}.json" for case in episode.cases}
        if {path.name for path in paths} != expected_names:
            raise ValueError(f"incomplete or unexpected histories in {directory}")
        for case in episode.cases:
            record = _record_from_history(directory / f"{case.trial_id}.json", case, seed_base)
            records[case.ordinal] = record
        completed[episode.episode_id] = {
            "episode_id": episode.episode_id,
            "topology_condition": episode.topology_condition,
            "repetition": episode.repetition,
            "signature_extension": episode.signature_extension,
            "history_count": len(episode.cases),
            "trial_ids": [case.trial_id for case in episode.cases],
            "fault_status": None,
            "recovery_status": None,
            "event": None,
            "recovered_from_episode_directory": True,
        }
        first_history = read_history(directory / f"{episode.cases[0].trial_id}.json")
        if first_history.fault_events:
            event = dict(first_history.fault_events[0])
            completed[episode.episode_id].update(
                {
                    "faulted_member": (event.get("members") or [None])[0],
                    "initial_primary": first_history.manifest.get("initial_primary"),
                    "new_primary": event.get("new_primary"),
                    "fault_status": event.get("status"),
                    "recovery_status": event.get("recovery_status"),
                    "event": event,
                }
            )
        if not _episode_is_valid(completed[episode.episode_id]):
            raise ValueError(
                f"existing RQ2 episode {episode.episode_id} did not verify fault application "
                "and recovery; archive the failed attempt before resuming"
            )
    return records, completed


def run_campaign(output_root: Path, *, resume: bool = False) -> dict[str, Any]:
    configurations = load_configurations(ROOT / "configs/configurations.json")
    campaign_config = load_json(ROOT / "configs/campaign.json")
    runtime_metadata = campaign_runtime_metadata(output_root)
    _require_frozen_provenance("rq2", runtime_metadata)
    episodes = plan_episodes()
    seed_base = int(campaign_config["seed_base"])
    campaign_directory = output_root / "rq2"
    manifest_path = campaign_directory / "campaign-manifest.json"
    episode_root = campaign_directory / "episodes"
    if manifest_path.exists() and not resume:
        raise ValueError(f"{manifest_path} already exists; pass --resume or choose a new output root")
    if resume and not manifest_path.is_file():
        raise ValueError("cannot resume RQ2 because campaign-manifest.json is missing")

    if resume:
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read existing RQ2 manifest: {error}") from error
        if not isinstance(previous, dict) or previous.get("campaign") != "rq2":
            raise ValueError("existing campaign manifest is not an RQ2 manifest")
        if previous.get("expected_case_count") != EXPECTED_HISTORY_COUNT:
            raise ValueError("existing RQ2 manifest uses a different history count")
        if previous.get("planned_episode_ids") != [episode.episode_id for episode in episodes]:
            raise ValueError("existing RQ2 manifest uses a different episode plan")
        if previous.get("seed_base") != seed_base:
            raise ValueError("existing RQ2 manifest uses a different seed base")
        for field in (
            "prediction_commit",
            "prediction_manifest_hash",
            "protocol_commit",
            "protocol_hash",
            "runner_commit",
            "runner_dirty",
            "software_versions",
            "image_digest",
        ):
            if previous.get(field) != runtime_metadata.get(field):
                raise ValueError(f"existing RQ2 manifest uses different {field} provenance")
        started_ns = int(previous.get("started_ns", time.monotonic_ns()))
        records_by_ordinal, completed_episodes = _load_completed_episodes(
            campaign_directory, episodes, seed_base=seed_base
        )
        previous_records = previous.get("records")
        if not isinstance(previous_records, list):
            raise ValueError("existing RQ2 manifest has no records list")
        previous_by_ordinal: dict[int, dict[str, Any]] = {}
        for record in previous_records:
            if (
                not isinstance(record, dict)
                or type(record.get("ordinal")) is not int
                or record["ordinal"] in previous_by_ordinal
            ):
                raise ValueError("existing RQ2 manifest has invalid or duplicate records")
            previous_by_ordinal[record["ordinal"]] = record
        if len(previous_records) != previous.get("completed_case_count"):
            raise ValueError("existing RQ2 manifest record count is inconsistent")
        for ordinal, previous_record in previous_by_ordinal.items():
            recovered = records_by_ordinal.get(ordinal)
            if (
                recovered is None
                or recovered.get("trial_id") != previous_record.get("trial_id")
                or recovered.get("history_hash") != previous_record.get("history_hash")
            ):
                raise ValueError("RQ2 episode directories disagree with a recorded history")
    else:
        if episode_root.exists() and any(episode_root.iterdir()):
            raise ValueError(f"{episode_root} already contains histories; pass --resume")
        campaign_directory.mkdir(parents=True, exist_ok=True)
        started_ns = time.monotonic_ns()
        records_by_ordinal = {}
        completed_episodes = {}

    seed_uris = tuple(
        value
        for value in os.environ.get(
            "MONGO_SEEDS",
            "mongodb://mongo1:27017,mongo2:27017,mongo3:27017/?replicaSet=rs0",
        ).split(";")
        if value
    )
    coordinator = HostFaultCoordinatorClient.from_environment()
    shutdown_requested = False

    def request_shutdown(_signal_number: int, _frame: Any) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

    previous_handlers = {
        number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
    }
    for number in previous_handlers:
        signal.signal(number, request_shutdown)

    status = "RUNNING"
    error_message: str | None = None
    manifest = _manifest(
        status=status,
        runtime_metadata=runtime_metadata,
        seed_base=seed_base,
        episodes=episodes,
        completed_records=records_by_ordinal,
        completed_episodes=completed_episodes,
        started_ns=started_ns,
        resumed=resume,
    )
    _write_json_atomic(manifest_path, manifest)

    try:
        for episode in episodes:
            if shutdown_requested:
                status = "INTERRUPTED"
                break
            if episode.episode_id in completed_episodes:
                continue
            histories, episode_record = run_episode(
                episode,
                configurations=configurations,
                seed_uris=seed_uris,
                runtime_metadata=runtime_metadata,
                coordinator=coordinator,
            )
            staging_root = campaign_directory / ".rq2-staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            stage = Path(
                tempfile.mkdtemp(prefix=f"{episode.episode_id}-", dir=staging_root)
            )
            final_episode_directory = episode_root / episode.episode_id
            try:
                episode_records = []
                for case_plan, history in zip(episode.cases, histories, strict=True):
                    stage_path = stage / f"{case_plan.trial_id}.json"
                    history_hash = write_history(stage_path, history)
                    final_path = final_episode_directory / stage_path.name
                    history.history_hash = history_hash
                    episode_records.append(
                        _record_for_history(
                            history,
                            final_path,
                            case_plan,
                            seed_base=seed_base,
                        )
                    )
                final_episode_directory.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage, final_episode_directory)
                try:
                    staging_root.rmdir()
                except OSError:
                    pass
            except Exception:
                for path in stage.glob("*"):
                    path.unlink(missing_ok=True)
                stage.rmdir()
                raise
            for record in episode_records:
                records_by_ordinal[int(record["ordinal"])] = record
            completed_episodes[episode.episode_id] = episode_record
            manifest = _manifest(
                status="RUNNING",
                runtime_metadata=runtime_metadata,
                seed_base=seed_base,
                episodes=episodes,
                completed_records=records_by_ordinal,
                completed_episodes=completed_episodes,
                started_ns=started_ns,
                resumed=resume,
            )
            _write_json_atomic(manifest_path, manifest)
            print(
                json.dumps(
                    {
                        "episode_id": episode.episode_id,
                        "topology_condition": episode.topology_condition,
                        "repetition": episode.repetition,
                        "completed_episodes": len(completed_episodes),
                        "completed_histories": len(records_by_ordinal),
                        "recovery_status": episode_record["recovery_status"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if not _episode_is_valid(episode_record):
                raise RuntimeError(
                    f"RQ2 episode {episode.episode_id} failed fault or recovery validation: "
                    f"fault_status={episode_record['fault_status']!r}, "
                    f"recovery_status={episode_record['recovery_status']!r}, "
                    f"error={episode_record['error']!r}"
                )
        else:
            status = "COMPLETE"
    except Exception as error:  # Persist episode-level failure and completed work.
        status = "FAILED"
        error_message = str(error)
        raise
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
        manifest = _manifest(
            status=status,
            runtime_metadata=runtime_metadata,
            seed_base=seed_base,
            episodes=episodes,
            completed_records=records_by_ordinal,
            completed_episodes=completed_episodes,
            started_ns=started_ns,
            resumed=resume,
            finished_ns=time.monotonic_ns() if status != "RUNNING" else None,
            error=error_message,
        )
        _write_json_atomic(manifest_path, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="resume only complete fault episodes")
    parser.add_argument("--output-root", type=Path, default=ROOT / "results/raw")
    args = parser.parse_args(argv)
    manifest = run_campaign(args.output_root, resume=args.resume)
    print(
        json.dumps(
            {
                "campaign": "rq2",
                "status": manifest["status"],
                "completed_histories": manifest["completed_case_count"],
                "expected_histories": manifest["expected_case_count"],
                "completed_episodes": manifest["completed_fault_episode_count"],
                "expected_episodes": manifest["fault_episode_count"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 130 if manifest["status"] == "INTERRUPTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
