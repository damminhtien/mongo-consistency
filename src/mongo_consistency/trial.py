"""Subject-client operations and trial-local evidence collection."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

from .driver import (
    ClientSettings,
    RoutingMonitor,
    address_text,
    collection_for,
    create_client,
    operation_record_from_events,
    session_cluster_time,
    session_operation_time,
    tagged_secondary,
)
from .models import History, OperationRecord
from .topology import DEFAULT_MEMBERS, TopologyOracle, TopologyState

TIMEOUT_POLICY = {
    "connect_ms": 2000,
    "server_selection_ms": 5000,
    "operation_ms": 5000,
    "write_concern_ms": 5000,
    "election_barrier_ms": 30000,
    "subtrial_ms": 60000,
}

NETWORK_ERRORS = frozenset(
    {
        "AutoReconnect",
        "ConnectionFailure",
        "ExecutionTimeout",
        "NetworkTimeout",
        "ServerSelectionTimeoutError",
    }
)
AMBIGUOUS_WRITE_ERRORS = frozenset(
    {"ExecutionTimeout", "NetworkTimeout", "WTimeoutError", "WriteConcernError"}
)
DEFINITIVE_WRITE_FAILURE_CODES = frozenset({13, 66, 10107, 11000, 121})


class SubtrialDeadlineExceeded(RuntimeError):
    """Raised before a subject operation would exceed its bounded trial."""


def classify_exception(
    error: Exception,
    kind: str,
    *,
    command_started: bool = False,
    response_received: bool = False,
) -> tuple[str, str | None]:
    """Classify availability only after checking whether a write reached MongoDB."""

    name = type(error).__name__
    code = getattr(error, "code", None)
    code_text = str(code) if code is not None else name
    if kind == "write":
        if not command_started:
            if name in NETWORK_ERRORS or "Timeout" in name or "Connection" in name:
                return "UNAVAILABLE", code_text
            return "HARNESS_ERROR", code_text
        if name in AMBIGUOUS_WRITE_ERRORS or not response_received:
            return "INDETERMINATE", code_text
        if code in DEFINITIVE_WRITE_FAILURE_CODES or name in {
            "DuplicateKeyError",
            "OperationFailure",
            "WriteError",
        }:
            return "UNAVAILABLE", code_text
        return "HARNESS_ERROR", code_text
    if name in NETWORK_ERRORS or "Timeout" in name or "Connection" in name:
        return "UNAVAILABLE", code_text
    if response_received and name in {"OperationFailure", "ExecutionTimeout"}:
        return "UNAVAILABLE", code_text
    return "HARNESS_ERROR", code_text


def _operation_timeouts() -> Any:
    pymongo = __import__("pymongo")
    return pymongo.timeout(TIMEOUT_POLICY["operation_ms"] / 1000)


def _session_identifier(session: Any) -> str:
    raw = session.session_id.get("id")
    try:
        return bytes(raw).hex()
    except (TypeError, ValueError):
        return str(raw)


def _member_from_address(address: str | None) -> str | None:
    if not address:
        return None
    host = address_text(address).rsplit(":", 1)[0].strip("[]").lower()
    for member in DEFAULT_MEMBERS:
        if host == member or host.startswith(f"{member}."):
            return member
    try:
        address_ip = socket.gethostbyname(host)
    except OSError:
        return None
    for member, uri in DEFAULT_MEMBERS.items():
        member_host = uri.split("//", 1)[1].split(":", 1)[0]
        try:
            if socket.gethostbyname(member_host) == address_ip:
                return member
        except OSError:
            continue
    return None


class MongoTrial:
    """Run subject operations through one session and diagnostics through direct clients."""

    def __init__(
        self,
        *,
        seed_uris: tuple[str, ...],
        configuration: dict[str, Any],
        trial_id: str,
        property_name: str,
        schedule_id: str,
        seed: int,
        runtime_metadata: dict[str, Any] | None = None,
        subtrial_deadline_seconds: float = 60.0,
        oracle: TopologyOracle | None = None,
    ) -> None:
        self.configuration = configuration
        self.trial_id = trial_id
        self.property_name = property_name
        self.schedule_id = schedule_id
        self.seed = seed
        self.started_ns = time.monotonic_ns()
        self.deadline_ns = self.started_ns + int(subtrial_deadline_seconds * 1_000_000_000)
        self.runtime_metadata = dict(runtime_metadata or {})
        self.database_name = f"mc_{trial_id.replace('-', '_')}"
        self.collection_name = "logical"
        self.document_id = f"{trial_id}/x"
        self.oracle = oracle or TopologyOracle()
        self.roles: dict[str, str | None] = {}
        self.monitor = RoutingMonitor(lambda address: self._role_for_address(address))
        self.client = create_client(
            ClientSettings(seed_uris=seed_uris),
            monitor=self.monitor,
        )
        self.session: Any | None = None
        self.operations: list[OperationRecord] = []
        self.fault_events: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}
        self.final_observation: dict[str, Any] | None = None
        self.precondition: dict[str, Any] = {"status": "SATISFIED", "checks": []}
        self.active_fault_members: set[str] = set()
        self.active_fault_event_id: str | None = None
        self.manifest: dict[str, Any] = {
            "schema_version": "manifest.v1",
            "trial_id": trial_id,
            "campaign_id": "unassigned",
            "configuration_id": configuration["id"],
            "read_concern": configuration["read_concern"],
            "write_concern": configuration["write_concern"],
            "causal_session": configuration["causal_session"],
            "property": property_name,
            "schedule_id": schedule_id,
            "seed": seed,
            "namespace": {
                "database": self.database_name,
                "collection": self.collection_name,
                "document_id": self.document_id,
            },
            "retry_reads": False,
            "retry_writes": False,
            "timeout_policy": dict(TIMEOUT_POLICY),
        }
        self.manifest.update(self.runtime_metadata)
        self.manifest["subtrial_started_ns"] = self.started_ns

    def __enter__(self) -> Self:
        self.ensure_deadline()
        pymongo = __import__("pymongo")
        self.session = self.client.start_session(
            causal_consistency=bool(self.configuration["causal_session"])
        )
        self.manifest["session_id"] = self.session_id
        self.manifest["driver_version"] = pymongo.version
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def session_id(self) -> str:
        return _session_identifier(self.session) if self.session is not None else "unstarted"

    @property
    def precondition_satisfied(self) -> bool:
        return self.precondition.get("status") == "SATISFIED"

    def close(self) -> None:
        if self.session is not None:
            self.session.end_session()
            self.session = None
        self.client.close()
        self.oracle.close()

    def _role_for_address(self, address: str) -> str | None:
        role = self.roles.get(address)
        if role in {"RSPrimary", "Primary"}:
            return "PRIMARY"
        if role in {"RSSecondary", "Secondary"}:
            return "SECONDARY"
        return role

    def refresh_driver_topology_cache(self) -> dict[str, str | None]:
        """Capture the driver's view for comparison, never as topology ground truth."""

        descriptions = self.client.topology_description.server_descriptions()
        self.roles = {
            address_text(address): getattr(description, "server_type_name", None)
            for address, description in descriptions.items()
        }
        self.manifest["driver_topology_cache"] = [
            {"address": address, "role": self._role_for_address(address)}
            for address in sorted(self.roles)
        ]
        return dict(self.roles)

    def topology_state(self) -> TopologyState:
        state = self.oracle.snapshot()
        self.record_diagnostic("topology-snapshot", state.to_dict())
        self.refresh_driver_topology_cache()
        return state

    def wait_for_stable_topology(self, timeout_seconds: float = 30.0) -> TopologyState:
        state = self.oracle.wait_for_stable(timeout_seconds)
        self.record_diagnostic("stable-topology", state.to_dict())
        self.refresh_driver_topology_cache()
        return state

    def primary_member(self) -> str | None:
        return self.oracle.snapshot().primary

    def record_diagnostic(self, name: str, payload: dict[str, Any]) -> None:
        self.diagnostics.append(
            {"name": name, "observed_at_ns": time.monotonic_ns(), "data": dict(payload)}
        )

    def record_precondition(
        self,
        name: str,
        *,
        satisfied: bool,
        expected: Any,
        actual: Any,
        details: dict[str, Any] | None = None,
    ) -> bool:
        check = {
            "name": name,
            "status": "SATISFIED" if satisfied else "PRECONDITION_MISS",
            "expected": expected,
            "actual": actual,
            "observed_at_ns": time.monotonic_ns(),
        }
        if details:
            check["details"] = dict(details)
        self.precondition["checks"].append(check)
        if not satisfied:
            self.precondition["status"] = "PRECONDITION_MISS"
        return satisfied

    def mark_precondition_miss(
        self,
        name: str,
        *,
        expected: Any,
        actual: Any,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.record_precondition(
            name,
            satisfied=False,
            expected=expected,
            actual=actual,
            details=details,
        )

    def set_fault_state(self, members: set[str], event_id: str | None) -> None:
        self.active_fault_members = set(members)
        self.active_fault_event_id = event_id if members else None

    def set_campaign(self, campaign_id: str) -> None:
        self.manifest["campaign_id"] = campaign_id

    def set_adversarial(self, adversarial: bool) -> None:
        self.manifest["adversarial"] = bool(adversarial)

    def set_steps(self, steps: dict[str, str]) -> None:
        self.manifest["property_steps"] = dict(steps)

    def add_fault_event(self, event: dict[str, Any]) -> None:
        self.fault_events.append(dict(event))

    def update_fault_event(self, event_id: str, updates: dict[str, Any]) -> None:
        for event in reversed(self.fault_events):
            if event.get("event_id") == event_id:
                event.update(updates)
                return

    def ensure_deadline(self) -> None:
        if time.monotonic_ns() >= self.deadline_ns:
            self.manifest["subtrial_deadline_exceeded"] = True
            raise SubtrialDeadlineExceeded(
                f"subtrial exceeded {TIMEOUT_POLICY['subtrial_ms']}ms deadline"
            )

    def remaining_seconds(self) -> float:
        return max(0.0, (self.deadline_ns - time.monotonic_ns()) / 1_000_000_000)

    def initialize(self) -> bool:
        """Create v0 through an independent direct client and verify all three copies."""

        try:
            state = self.wait_for_stable_topology(TIMEOUT_POLICY["election_barrier_ms"] / 1000)
        except Exception as error:  # noqa: BLE001 - failure to establish setup is explicit.
            self.mark_precondition_miss(
                "initial-stable-topology",
                expected="one primary and two secondaries",
                actual={"error": str(error)},
            )
            return False
        self.record_precondition(
            "initial-stable-topology",
            satisfied=state.stable,
            expected="one primary and two secondaries",
            actual={"primary": state.primary, "secondaries": list(state.secondaries)},
        )
        if not state.stable or state.primary is None:
            return False
        init_update = {
            "write_id": "init",
            "version": 0,
            "effect": "initial",
            "parent_write_id": None,
            "depends_on_read_id": None,
            "depends_on_version": None,
        }
        started_ns = time.monotonic_ns()
        try:
            self.oracle.setup_write(
                state.primary,
                self.database_name,
                self.collection_name,
                self.document_id,
                init_update,
                write_concern="majority",
                replace=True,
            )
            setup_result = {"status": "SUCCESS", "member": state.primary}
        except Exception as error:  # noqa: BLE001 - setup writes are not subject operations.
            setup_result = {
                "status": "UNAVAILABLE",
                "member": state.primary,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        setup_result["start_ns"] = started_ns
        setup_result["end_ns"] = time.monotonic_ns()
        self.record_diagnostic("initialize-logical-document", setup_result)
        if setup_result["status"] != "SUCCESS":
            self.mark_precondition_miss(
                "initial-document-created",
                expected="version 0 created with majority acknowledgement",
                actual=setup_result,
            )
            return False
        observation = self.oracle.wait_for_document_convergence(
            self.database_name,
            self.collection_name,
            self.document_id,
            timeout_seconds=min(
                TIMEOUT_POLICY["election_barrier_ms"] / 1000,
                self.remaining_seconds(),
            ),
        )
        self.record_diagnostic("initial-v0-observation", observation)
        members = observation.get("members", {})
        all_at_v0 = bool(observation.get("converged")) and all(
            value.get("observed_versions") == [0]
            and value.get("observed_write_ids") == ["init"]
            for value in members.values()
        ) and len(members) == 3
        self.record_precondition(
            "all-members-at-v0",
            satisfied=all_at_v0,
            expected={"observed_versions": [0], "observed_write_ids": ["init"]},
            actual={
                member: {
                    "reachable": value.get("reachable"),
                    "observed_versions": value.get("observed_versions"),
                    "observed_write_ids": value.get("observed_write_ids"),
                }
                for member, value in members.items()
            },
        )
        return all_at_v0

    def _collection(
        self,
        requested_member: str | None = None,
        *,
        force_primary: bool = False,
    ) -> Any:
        preference = None
        if requested_member and not force_primary:
            preference = tagged_secondary(requested_member)
        elif force_primary:
            preference = __import__("pymongo").read_preferences.Primary()
        return collection_for(
            self.client,
            self.database_name,
            self.collection_name,
            read_concern=self.configuration["read_concern"],
            write_concern=self.configuration["write_concern"],
            read_preference=preference,
        )

    def _execute(
        self,
        operation: OperationRecord,
        action: Callable[[], Any],
    ) -> tuple[OperationRecord, Any | None]:
        if self.session is None:
            raise RuntimeError("subject session has not started")
        self.ensure_deadline()
        wrapper_start = time.monotonic_ns()
        operation.start_ns = wrapper_start
        operation.cluster_time_before = session_cluster_time(self.session)
        operation.operation_time_before = session_operation_time(self.session)
        operation.fault_state = {
            "isolated_members": sorted(self.active_fault_members),
            "event_id": self.active_fault_event_id,
        }
        result: Any | None = None
        caught_error: Exception | None = None
        self.monitor.attach(operation.operation_id)
        try:
            with _operation_timeouts():
                result = action()
            operation.operation_status = "SUCCESS"
        except Exception as error:  # noqa: BLE001 - PyMongo has several timeout classes.
            caught_error = error
        finally:
            self.monitor.detach()
        events = self.monitor.events_for(operation.operation_id)
        operation_record_from_events(operation, events)
        member = _member_from_address(operation.actual_server_address)
        if member is not None:
            role_observation = self.oracle.member_state(member)
            operation.actual_role_observed_at_ns = role_observation.get("observed_at_ns")
            operation.actual_role = (
                role_observation.get("role")
                if role_observation.get("reachable") is True
                else None
            )
            self.record_diagnostic(
                "subject-route-role",
                {
                    "operation_id": operation.operation_id,
                    "member": member,
                    "server_address": operation.actual_server_address,
                    "direct_role_observation": role_observation,
                    "driver_reported_role": operation.driver_reported_role,
                },
            )
        wrapper_end = time.monotonic_ns()
        if operation.end_ns is None:
            operation.end_ns = wrapper_end
        if operation.start_ns is None:
            operation.start_ns = wrapper_start
        operation.cluster_time_after = session_cluster_time(self.session)
        operation.operation_time_after = session_operation_time(self.session)
        for event in reversed(events):
            if event.get("read_concern"):
                operation.read_concern = str(event["read_concern"].get("level", operation.read_concern))
            if event.get("write_concern"):
                operation.write_concern = str(event["write_concern"].get("w", operation.write_concern))
            if event.get("after_cluster_time") is not None:
                operation.after_cluster_time = event["after_cluster_time"]
            if event.get("status") in {"SUCCESS", "ERROR"}:
                break
        if caught_error is not None:
            operation.response_received = (
                type(caught_error).__name__
                in {
                    "OperationFailure",
                    "WTimeoutError",
                    "WriteConcernError",
                    "DuplicateKeyError",
                    "WriteError",
                }
                or any(event.get("status") == "SUCCESS" for event in events)
            )
            operation.operation_status, operation.error_code = classify_exception(
                caught_error,
                operation.kind,
                command_started=operation.command_started,
                response_received=operation.response_received,
            )
            operation.error_message = str(caught_error)
        elif wrapper_end >= self.deadline_ns:
            operation.operation_status = (
                "INDETERMINATE" if operation.kind == "write" else "UNAVAILABLE"
            )
            operation.timeout_category = "subtrial_deadline"
            operation.response_received = False
            self.manifest["subtrial_deadline_exceeded"] = True
        self.operations.append(operation)
        return operation, result

    def setup_write(
        self,
        member: str,
        *,
        write_id: str,
        version: int,
        write_concern: str = "majority",
        parent_write_id: str | None = None,
        depends_on_read_id: str | None = None,
        depends_on_version: int | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Write a schedule-preparation version outside the subject session."""

        update = {
            "write_id": write_id,
            "version": version,
            "effect": f"set-v{version}",
            "parent_write_id": parent_write_id,
            "depends_on_read_id": depends_on_read_id,
            "depends_on_version": depends_on_version,
        }
        started_ns = time.monotonic_ns()
        try:
            response = self.oracle.setup_write(
                member,
                self.database_name,
                self.collection_name,
                self.document_id,
                update,
                write_concern=write_concern,
                replace=replace,
            )
            result = {"status": "SUCCESS", "member": member, **response}
        except Exception as error:  # noqa: BLE001 - setup failures are classified as preconditions.
            result = {
                "status": "UNAVAILABLE",
                "member": member,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        result.update({"start_ns": started_ns, "end_ns": time.monotonic_ns()})
        self.record_diagnostic(f"setup-write-{write_id}", result)
        return result

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
        update = {
            "write_id": write_id,
            "version": intended_version,
            "effect": f"set-v{intended_version}",
            "parent_write_id": parent_write_id,
            "depends_on_read_id": depends_on_read_id,
            "depends_on_version": depends_on_version,
        }
        operation = OperationRecord(
            operation_id=operation_id,
            kind="write",
            key="x",
            trial_id=self.trial_id,
            property=self.property_name,
            session_id=self.session_id,
            causal_session=bool(self.configuration["causal_session"]),
            write_concern=self.configuration["write_concern"],
            requested_member="primary",
            intended_version=intended_version,
            write_id=write_id,
            parent_write_id=parent_write_id,
            depends_on_read_id=depends_on_read_id,
            depends_on_version=depends_on_version,
            dependency_metadata={
                "parent_write_id": parent_write_id,
                "depends_on_read_id": depends_on_read_id,
                "depends_on_version": depends_on_version,
            },
            fault_event_id=fault_event_id,
        )

        def action() -> Any:
            result = self._collection().update_one(
                {"_id": self.document_id},
                {"$push": {"updates": update}},
                upsert=False,
                session=self.session,
            )
            if result.matched_count != 1:
                raise RuntimeError("logical document was not found")
            return result

        return self._execute(operation, action)[0]

    def read(
        self,
        operation_id: str,
        *,
        requested_member: str | None = None,
        force_primary: bool = False,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        operation = OperationRecord(
            operation_id=operation_id,
            kind="read",
            key="x",
            trial_id=self.trial_id,
            property=self.property_name,
            session_id=self.session_id,
            causal_session=bool(self.configuration["causal_session"]),
            read_concern=self.configuration["read_concern"],
            requested_member=requested_member or "primary",
            fault_event_id=fault_event_id,
        )

        def action() -> Any:
            document = self._collection(
                requested_member,
                force_primary=force_primary,
            ).find_one({"_id": self.document_id}, session=self.session)
            if document is None:
                operation.observed_document_exists = False
                operation.observed_updates = ()
                return None
            operation.observed_document_exists = True
            updates = document.get("updates")
            if not isinstance(updates, list):
                raise RuntimeError("logical document or update list is missing")
            operation.observed_updates = tuple(dict(update) for update in updates)
            versions = sorted(
                int(update["version"])
                for update in operation.observed_updates
                if isinstance(update.get("version"), int)
                and not isinstance(update.get("version"), bool)
            )
            operation.observed_versions = tuple(versions)
            operation.observed_write_ids = tuple(
                str(update["write_id"])
                for update in operation.observed_updates
                if isinstance(update.get("write_id"), str)
            )
            operation.observed_version = max(versions) if versions else None
            return document

        return self._execute(operation, action)[0]

    def verify_requested_route(self, operation: OperationRecord, expected_member: str) -> bool:
        actual_member = _member_from_address(operation.actual_server_address)
        satisfied = actual_member == expected_member
        self.record_precondition(
            f"actual-route-{operation.operation_id}",
            satisfied=satisfied,
            expected=expected_member,
            actual={
                "member": actual_member,
                "server_address": operation.actual_server_address,
                "role": operation.actual_role,
            },
        )
        return satisfied

    def capture_final_observation(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Read all member copies independently after fault cleanup."""

        topology: dict[str, Any]
        try:
            state = self.oracle.wait_for_stable(timeout_seconds)
            topology = state.to_dict()
        except Exception as error:  # noqa: BLE001 - convergence failure is retained as data.
            topology = {"stable": False, "error": str(error), "observed_at_ns": time.monotonic_ns()}
        observation = self.oracle.wait_for_document_convergence(
            self.database_name,
            self.collection_name,
            self.document_id,
            timeout_seconds=min(timeout_seconds, self.remaining_seconds()),
        )
        observation["topology"] = topology
        if topology.get("stable") is not True:
            observation["converged"] = False
            observation.setdefault(
                "error",
                "replica-set topology was not stable before final observation",
            )
        self.final_observation = observation
        self.record_diagnostic("post-heal-final-observation", observation)
        return observation

    def history(self) -> History:
        self.manifest["subtrial_finished_ns"] = time.monotonic_ns()
        self.refresh_driver_topology_cache()
        self.manifest["session_id"] = self.session_id
        if not self.precondition.get("checks"):
            self.mark_precondition_miss(
                "schedule-preconditions-recorded",
                expected="at least one explicit precondition check",
                actual="none",
            )
        return History(
            manifest=dict(self.manifest),
            operations=list(self.operations),
            precondition={
                "status": self.precondition["status"],
                "checks": [dict(check) for check in self.precondition["checks"]],
            },
            diagnostics=list(self.diagnostics),
            final_observation=(
                dict(self.final_observation) if self.final_observation is not None else None
            ),
            fault_events=list(self.fault_events),
            metadata=dict(self.metadata),
        )
