"""Protocol-v2 topology plans and pair-level experimental control checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TopologyPlan:
    """Named physical members required by one preregistered contrast."""

    initial_primary: str
    isolation_target: str
    expected_new_primary: str | None
    subject_read_member: str | None = None
    first_write_member: str | None = None
    second_write_member: str | None = None
    election_guard_member: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


TOPOLOGY_PLANS = {
    "M1": TopologyPlan(
        initial_primary="mongo3",
        isolation_target="mongo1",
        expected_new_primary=None,
        subject_read_member="mongo1",
        first_write_member="mongo3",
    ),
    "M2": TopologyPlan(
        initial_primary="mongo3",
        isolation_target="mongo3",
        expected_new_primary="mongo2",
        subject_read_member="mongo3",
        first_write_member="mongo3",
        second_write_member="mongo2",
        election_guard_member="mongo1",
    ),
    "M3": TopologyPlan(
        initial_primary="mongo3",
        isolation_target="mongo3",
        expected_new_primary="mongo2",
        first_write_member="mongo3",
        second_write_member="mongo2",
        election_guard_member="mongo1",
    ),
}

CONTRAST_CONFIGURATIONS = {
    "M1": ("C5", "C6"),
    "M2": ("C8", "C5"),
    "M3": ("C3", "C6"),
}

EXPECTED_ROUTES = {
    "M1": {"write": "mongo3", "read": "mongo1"},
    "M2": {"read": "mongo3", "write": "mongo2"},
    "M3": {"first_write": "mongo3", "second_write": "mongo2"},
}

TOPOLOGY_BARRIER_SECONDS = 45.0
ELECTION_FREEZE_SECONDS = 120


def normalize_topology(
    oracle: Any,
    controller: Any,
    plan: TopologyPlan,
    *,
    event_id: str,
    timeout_seconds: float = TOPOLOGY_BARRIER_SECONDS,
) -> dict[str, Any]:
    """Heal, unfreeze, and establish the named initial primary using observed barriers."""

    members = tuple(sorted(oracle.members))
    controller.heal_many(list(members), event_id)
    health = controller.health()
    if any(health.get(member, {}).get("replication_isolated") is not False for member in members):
        raise RuntimeError(f"topology normalization could not heal all partitions: {health!r}")

    initial = oracle.wait_for_stable(timeout_seconds)
    initial_convergence = oracle.wait_for_data_convergence(timeout_seconds)
    for member in initial.secondaries:
        oracle.freeze_member(member, 0)
    state = oracle.wait_for_primary(initial.primary, timeout_seconds)

    frozen: list[str] = []
    step_down_error: dict[str, str] | None = None
    if state.primary != plan.initial_primary:
        if plan.initial_primary not in state.secondaries:
            raise RuntimeError(
                f"desired initial primary {plan.initial_primary} is not an observed secondary"
            )
        for member in state.secondaries:
            if member == plan.initial_primary:
                continue
            oracle.freeze_member(member, ELECTION_FREEZE_SECONDS)
            frozen.append(member)
        try:
            oracle.step_down_primary(state.primary)
        except Exception as error:  # noqa: BLE001 - direct-role barrier confirms completion.
            # The command commonly loses its connection after the old primary steps down.
            step_down_error = {"type": type(error).__name__, "message": str(error)}
        state = oracle.wait_for_primary(plan.initial_primary, timeout_seconds)
        for member in frozen:
            oracle.freeze_member(member, 0)
        state = oracle.wait_for_primary(plan.initial_primary, timeout_seconds)

    if not state.stable or state.primary != plan.initial_primary:
        raise RuntimeError(
            f"topology normalization did not establish {plan.initial_primary}: {state.to_dict()}"
        )
    convergence = oracle.wait_for_data_convergence(timeout_seconds)
    return {
        **state.to_dict(),
        "step_down_command_error": step_down_error,
        "initial_data_convergence": initial_convergence,
        "data_convergence": convergence,
    }


def _diagnostics(history: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in history.get("diagnostics", []):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            data = item.get("data")
            if isinstance(data, dict):
                result.setdefault(item["name"], []).append(data)
    return result


def _member_from_address(address: Any) -> str | None:
    if not isinstance(address, str) or not address:
        return None
    host = address.rsplit(":", 1)[0].strip("[]").lower()
    for member in ("mongo1", "mongo2", "mongo3"):
        if host == member or host.startswith(f"{member}."):
            return member
    return None


def _operation(history: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    for operation in history.get("operations", []):
        if isinstance(operation, dict) and operation.get("operation_id") == operation_id:
            return operation
    return None


def _semantic_state(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    members = value.get("members")
    if not isinstance(members, dict):
        return None
    state: dict[str, dict[str, Any]] = {}
    for member in ("mongo1", "mongo2", "mongo3"):
        observation = members.get(member)
        if not isinstance(observation, dict):
            return None
        state[member] = {
            "reachable": observation.get("reachable"),
            "observed_versions": observation.get("observed_versions"),
            "observed_write_ids": observation.get("observed_write_ids"),
        }
    return state


def _prestate(history: dict[str, Any], contrast_id: str) -> dict[str, dict[str, Any]] | None:
    diagnostics = _diagnostics(history)
    snapshots = diagnostics.get("rq3-prestate", [])
    wanted_stage = {"M1": "before-read", "M2": "before-read", "M3": "before-first-write"}[contrast_id]
    for snapshot in snapshots:
        if snapshot.get("stage") == wanted_stage:
            return _semantic_state(snapshot)
    return None


def observed_controls(history: dict[str, Any], contrast_id: str) -> dict[str, Any]:
    """Extract control facts from one raw arm without consulting its outcome."""

    manifest = history.get("manifest", {})
    diagnostics = _diagnostics(history)
    initial = diagnostics.get("rq3-initial-topology", [{}])[0]
    normalization = manifest.get("rq3_normalization")
    initial_members = initial.get("members", {}) if isinstance(initial, dict) else {}
    faults = [
        event
        for event in history.get("fault_events", [])
        if isinstance(event, dict)
        and event.get("action") == "isolate"
        and event.get("status") == "APPLIED"
    ]
    election = diagnostics.get("majority-side-election", [{}])[-1]
    routes = {
        operation_id: _member_from_address(
            (_operation(history, operation_id) or {}).get("actual_server_address")
        )
        for operation_id in EXPECTED_ROUTES[contrast_id]
    }
    setup = diagnostics.get("setup-write-w1", [{}])[-1] if contrast_id == "M2" else None
    setup_events = setup.get("command_events", []) if isinstance(setup, dict) else []
    setup_write_concern = next(
        (
            event.get("write_concern")
            for event in reversed(setup_events)
            if isinstance(event, dict) and isinstance(event.get("write_concern"), dict)
        ),
        None,
    )
    return {
        "topology_plan": manifest.get("rq3_topology_plan"),
        "initial_primary": initial.get("primary") if isinstance(initial, dict) else None,
        "initial_roles": {
            member: item.get("role")
            for member, item in initial_members.items()
            if isinstance(item, dict)
        },
        "isolation_targets": sorted(faults[0].get("members", [])) if faults else [],
        "new_primary": election.get("new_primary") if isinstance(election, dict) else None,
        "routes": routes,
        "setup_write": (
            {
                "operation_id": setup.get("operation_id"),
                "write_concern": setup_write_concern,
                "actual_server_address": setup.get("actual_server_address"),
                "member": _member_from_address(setup.get("actual_server_address")),
                "command_started": setup.get("command_started"),
            }
            if isinstance(setup, dict)
            else None
        ),
        "prestate": _prestate(history, contrast_id),
        "initial_document_state": _semantic_state(
            diagnostics.get("initial-v0-observation", [{}])[0]
        ),
        "normalization": normalization,
        "pair_seed": manifest.get("rq3_pair_seed"),
        "schedule_id": manifest.get("schedule_id"),
        "configuration_id": manifest.get("configuration_id"),
        "precondition_status": history.get("precondition", {}).get("status"),
    }


def arm_control_errors(
    history: dict[str, Any],
    contrast_id: str,
    configuration_id: str | None = None,
    configurations: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Return harness-control failures for one arm; consistency outcome is ignored."""

    plan = TOPOLOGY_PLANS[contrast_id]
    observed = observed_controls(history, contrast_id)
    errors: list[str] = []
    if observed["topology_plan"] != plan.to_dict():
        errors.append("topology plan differs from preregistered plan")
    normalization = observed["normalization"]
    if (
        not isinstance(normalization, dict)
        or normalization.get("primary") != plan.initial_primary
        or normalization.get("stable") is not True
        or {
            member: state.get("role")
            for member, state in normalization.get("members", {}).items()
            if isinstance(state, dict)
        }
        != {
            member: "PRIMARY" if member == plan.initial_primary else "SECONDARY"
            for member in ("mongo1", "mongo2", "mongo3")
        }
    ):
        errors.append("pre-arm topology normalization was not verified")
    if observed["initial_primary"] != plan.initial_primary:
        errors.append(f"initial primary is {observed['initial_primary']!r}, expected {plan.initial_primary}")
    if observed["initial_roles"] != {
        "mongo1": "SECONDARY" if plan.initial_primary != "mongo1" else "PRIMARY",
        "mongo2": "SECONDARY" if plan.initial_primary != "mongo2" else "PRIMARY",
        "mongo3": "SECONDARY" if plan.initial_primary != "mongo3" else "PRIMARY",
    }:
        errors.append("initial direct-member roles do not match the topology plan")
    if observed["isolation_targets"] != [plan.isolation_target]:
        errors.append("isolation target differs from the topology plan")
    if plan.expected_new_primary is not None and observed["new_primary"] != plan.expected_new_primary:
        errors.append(
            f"majority-side primary is {observed['new_primary']!r}, "
            f"expected {plan.expected_new_primary}"
        )
    for operation_id, expected_member in EXPECTED_ROUTES[contrast_id].items():
        if observed["routes"].get(operation_id) != expected_member:
            errors.append(
                f"{operation_id} route is {observed['routes'].get(operation_id)!r}, "
                f"expected {expected_member}"
            )
    if contrast_id == "M2":
        setup = observed["setup_write"]
        if not isinstance(setup, dict):
            errors.append("M2 setup W1 command event is missing")
        else:
            concern = setup.get("write_concern")
            if (
                setup.get("operation_id") != "setup_write"
                or setup.get("command_started") is not True
                or setup.get("member") != "mongo3"
                or not isinstance(concern, dict)
                or concern.get("w") != 1
            ):
                errors.append("M2 setup W1 command event does not prove w:1 on mongo3")
    if observed["prestate"] is None:
        errors.append("semantic pre-state snapshot is missing")
    initial_document_state = observed["initial_document_state"]
    if initial_document_state != {
        member: {
            "reachable": True,
            "observed_versions": [0],
            "observed_write_ids": ["init"],
        }
        for member in ("mongo1", "mongo2", "mongo3")
    }:
        errors.append("initial document state is not v0 on all three members")
    if observed["precondition_status"] != "SATISFIED":
        errors.append("one or more harness preconditions failed")
    manifest = history.get("manifest", {})
    manifest_configuration_id = manifest.get("configuration_id")
    expected_configuration_id = configuration_id or manifest_configuration_id
    if manifest_configuration_id != expected_configuration_id:
        errors.append("history configuration ID differs from its planned arm")
    if expected_configuration_id not in CONTRAST_CONFIGURATIONS[contrast_id]:
        errors.append("configuration ID is outside the registered contrast")
    if configurations is not None:
        configuration = configurations.get(expected_configuration_id)
        if not isinstance(configuration, dict):
            errors.append("registered configuration settings are missing")
        else:
            for field in ("read_concern", "write_concern", "causal_session"):
                if manifest.get(field) != configuration.get(field):
                    errors.append(f"{field} differs from the registered configuration")
    if manifest.get("adversarial") is not True:
        errors.append("history is not an adversarial protocol-v2 arm")
    return errors


def pair_control(
    contrast_id: str,
    pair_id: str,
    histories: dict[str, dict[str, Any]],
    *,
    configurations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a pair record and derive validity from raw arms, never outcomes."""

    expected_configurations = CONTRAST_CONFIGURATIONS[contrast_id]
    desired = {
        **TOPOLOGY_PLANS[contrast_id].to_dict(),
        "routes": dict(EXPECTED_ROUTES[contrast_id]),
    }
    arms: dict[str, Any] = {}
    all_errors: list[str] = []
    seeds: set[int] = set()
    schedules: set[str] = set()
    prestates: list[dict[str, dict[str, Any]] | None] = []
    for configuration_id in expected_configurations:
        history = histories.get(configuration_id)
        if history is None:
            arms[configuration_id] = {"present": False, "invalid_reasons": ["history missing"]}
            all_errors.append(f"{configuration_id}: history missing")
            continue
        observed = observed_controls(history, contrast_id)
        errors = arm_control_errors(
            history,
            contrast_id,
            configuration_id,
            configurations,
        )
        arms[configuration_id] = {
            "present": True,
            "observed": observed,
            "invalid_reasons": errors,
        }
        all_errors.extend(f"{configuration_id}: {error}" for error in errors)
        if type(observed.get("pair_seed")) is int:
            seeds.add(observed["pair_seed"])
        if isinstance(observed.get("schedule_id"), str):
            schedules.add(observed["schedule_id"])
        prestates.append(observed.get("prestate"))
        if history.get("manifest", {}).get("rq3_pair_id") != pair_id:
            all_errors.append(f"{configuration_id}: pair ID differs")
    if len(seeds) != 1:
        all_errors.append("arms do not share one integer pair seed")
    if len(schedules) != 1:
        all_errors.append("arms do not share one registered schedule")
    if len(prestates) == 2 and (prestates[0] is None or prestates[0] != prestates[1]):
        all_errors.append("arms do not have the same semantic pre-state")
    return {
        "contrast_id": contrast_id,
        "pair_id": pair_id,
        "pair_seed": next(iter(seeds)) if len(seeds) == 1 else None,
        "desired": desired,
        "observed": arms,
        "control_valid": not all_errors,
        "invalid_reasons": all_errors,
    }
