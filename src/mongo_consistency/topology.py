"""Direct-member topology inspection and independent document observation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .driver import require_pymongo

DEFAULT_MEMBERS = {
    "mongo1": "mongodb://mongo1:27017/",
    "mongo2": "mongodb://mongo2:27017/",
    "mongo3": "mongodb://mongo3:27017/",
}
SETUP_WRITE_CONCERN_TIMEOUT_MS = 5000
SETUP_WRITE_SOCKET_TIMEOUT_MS = 7000


class TopologyError(RuntimeError):
    """The direct-member oracle could not establish a required topology state."""


@dataclass(frozen=True)
class TopologyState:
    """One observation from direct connections to all configured members."""

    primary: str | None
    secondaries: tuple[str, ...]
    members: dict[str, dict[str, Any]]
    stable: bool
    observed_at_ns: int
    set_name: str | None = None
    election_id: str | None = None
    set_version: int | None = None
    last_committed_op_time: dict[str, int] | None = None
    term: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "secondaries": list(self.secondaries),
            "members": {name: dict(value) for name, value in self.members.items()},
            "stable": self.stable,
            "observed_at_ns": self.observed_at_ns,
            "set_name": self.set_name,
            "election_id": self.election_id,
            "set_version": self.set_version,
            "last_committed_op_time": (
                dict(self.last_committed_op_time)
                if self.last_committed_op_time is not None
                else None
            ),
            "term": self.term,
        }


class TopologyOracle:
    """Inspect each member directly instead of trusting the driver's topology cache."""

    def __init__(
        self,
        members: dict[str, str] | None = None,
        *,
        connect_timeout_ms: int = 2000,
        server_selection_timeout_ms: int = 2000,
        socket_timeout_ms: int = 2000,
        setup_write_socket_timeout_ms: int = SETUP_WRITE_SOCKET_TIMEOUT_MS,
        client_factory: Any | None = None,
    ) -> None:
        self.members = dict(members or DEFAULT_MEMBERS)
        if len(self.members) != 3:
            raise ValueError("the experiment requires exactly three data-bearing members")
        self.connect_timeout_ms = connect_timeout_ms
        self.server_selection_timeout_ms = server_selection_timeout_ms
        self.socket_timeout_ms = socket_timeout_ms
        self.setup_write_socket_timeout_ms = setup_write_socket_timeout_ms
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}
        self._setup_clients: dict[str, Any] = {}

    def _new_client(self, member: str, *, socket_timeout_ms: int) -> Any:
        if member not in self.members:
            raise TopologyError(f"unknown replica-set member: {member}")
        factory = self._client_factory
        if factory is None:
            pymongo = require_pymongo()
            factory = pymongo.MongoClient
        return factory(
            self.members[member],
            directConnection=True,
            connectTimeoutMS=self.connect_timeout_ms,
            serverSelectionTimeoutMS=self.server_selection_timeout_ms,
            socketTimeoutMS=socket_timeout_ms,
            retryReads=False,
            retryWrites=False,
        )

    def _client(self, member: str) -> Any:
        if member not in self._clients:
            self._clients[member] = self._new_client(
                member,
                socket_timeout_ms=self.socket_timeout_ms,
            )
        return self._clients[member]

    def _setup_client(self, member: str) -> Any:
        if member not in self._setup_clients:
            self._setup_clients[member] = self._new_client(
                member,
                socket_timeout_ms=self.setup_write_socket_timeout_ms,
            )
        return self._setup_clients[member]

    @staticmethod
    def _op_time(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        timestamp = value.get("ts")
        if timestamp is None:
            return None
        seconds = getattr(timestamp, "time", None)
        increment = getattr(timestamp, "inc", None)
        if isinstance(seconds, int) and isinstance(increment, int):
            return {"seconds": seconds, "increment": increment}
        if isinstance(timestamp, dict):
            seconds = timestamp.get("t")
            increment = timestamp.get("i")
            if isinstance(seconds, int) and isinstance(increment, int):
                return {"seconds": seconds, "increment": increment}
        return None

    def _hello(self, member: str) -> dict[str, Any]:
        reply = self._client(member).admin.command({"hello": 1})
        if not isinstance(reply, dict):
            raise TopologyError(f"{member} returned a non-object hello response")
        if reply.get("setName") != "rs0":
            raise TopologyError(f"{member} belongs to replica set {reply.get('setName')!r}")
        return reply

    def member_state(self, member: str) -> dict[str, Any]:
        """Return one member's role and reachability from an uncached direct hello."""

        observed_at_ns = time.monotonic_ns()
        try:
            hello = self._hello(member)
        except Exception as error:  # noqa: BLE001 - reachability is data for the caller.
            return {
                "member": member,
                "reachable": False,
                "role": "UNKNOWN",
                "observed_at_ns": observed_at_ns,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        if hello.get("isWritablePrimary", hello.get("ismaster", False)):
            role = "PRIMARY"
        elif hello.get("secondary"):
            role = "SECONDARY"
        elif hello.get("arbiterOnly"):
            role = "ARBITER"
        else:
            role = "OTHER"
        return {
            "member": member,
            "reachable": True,
            "role": role,
            "observed_at_ns": observed_at_ns,
            "set_name": hello.get("setName"),
            "me": hello.get("me"),
            "election_id": str(hello["electionId"]) if hello.get("electionId") is not None else None,
            "set_version": hello.get("setVersion"),
            "last_write_op_time": self._op_time(
                hello.get("lastWrite", {}).get("opTime")
                if isinstance(hello.get("lastWrite"), dict)
                else None
            ),
        }

    def snapshot(self) -> TopologyState:
        """Poll all members directly and report stable roles only when observed."""

        observed_at_ns = time.monotonic_ns()
        states = {member: self.member_state(member) for member in self.members}
        primaries = [name for name, value in states.items() if value["role"] == "PRIMARY"]
        secondaries = tuple(
            sorted(name for name, value in states.items() if value["role"] == "SECONDARY")
        )
        stable = (
            len(self.members) == 3
            and all(value["reachable"] for value in states.values())
            and len(primaries) == 1
            and len(secondaries) == 2
        )
        primary = primaries[0] if len(primaries) == 1 else None
        set_names = {
            str(value["set_name"])
            for value in states.values()
            if value.get("set_name") is not None
        }
        election_ids = {
            str(value["election_id"])
            for value in states.values()
            if value.get("election_id") is not None
        }
        set_versions = {
            int(value["set_version"])
            for value in states.values()
            if isinstance(value.get("set_version"), int)
        }
        last_committed = None
        term = None
        if primary is not None and states[primary]["reachable"]:
            try:
                status = self._client(primary).admin.command({"replSetGetStatus": 1})
                optimes = status.get("optimes", {})
                last_committed = self._op_time(optimes.get("lastCommittedOpTime"))
                raw_term = status.get("term")
                if isinstance(raw_term, int) and not isinstance(raw_term, bool):
                    term = raw_term
            except Exception:  # noqa: BLE001 - this field is optional diagnostic data.
                pass
        return TopologyState(
            primary=primary,
            secondaries=secondaries,
            members=states,
            stable=stable,
            observed_at_ns=observed_at_ns,
            set_name=next(iter(set_names)) if len(set_names) == 1 else None,
            election_id=next(iter(election_ids)) if len(election_ids) == 1 else None,
            set_version=next(iter(set_versions)) if len(set_versions) == 1 else None,
            last_committed_op_time=last_committed,
            term=term,
        )

    def wait_for_stable(self, timeout_seconds: float = 30.0) -> TopologyState:
        deadline = time.monotonic() + timeout_seconds
        last_state: TopologyState | None = None
        while time.monotonic() < deadline:
            last_state = self.snapshot()
            if last_state.stable:
                return last_state
            time.sleep(0.2)
        raise TopologyError(f"expected one primary and two secondaries; last={last_state}")

    def wait_for_majority_primary(
        self,
        isolated_member: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        """Observe election on the two connected members, excluding the isolated node."""

        if isolated_member not in self.members:
            raise TopologyError(f"unknown isolated member: {isolated_member}")
        majority_members = [member for member in self.members if member != isolated_member]
        deadline = time.monotonic() + timeout_seconds
        last_roles: dict[str, str] = {}
        while time.monotonic() < deadline:
            last_roles = {
                member: self.member_state(member)["role"] for member in majority_members
            }
            elected = [member for member, role in last_roles.items() if role == "PRIMARY"]
            secondary_count = sum(role == "SECONDARY" for role in last_roles.values())
            if len(elected) == 1 and secondary_count == 1:
                return elected[0]
            time.sleep(0.2)
        raise TopologyError(
            f"majority side did not elect one primary and one secondary: {last_roles}"
        )

    @staticmethod
    def _document_observation(member: str, document: dict[str, Any] | None) -> dict[str, Any]:
        raw_updates = document.get("updates") if isinstance(document, dict) else None
        observation_valid = isinstance(raw_updates, list) and all(
            isinstance(item, dict) for item in raw_updates
        )
        normalized = (
            [dict(item) for item in raw_updates]
            if observation_valid
            else []
        )
        versions = [
            int(item["version"])
            for item in normalized
            if isinstance(item.get("version"), int)
            and not isinstance(item.get("version"), bool)
        ]
        write_ids = [
            str(item["write_id"])
            for item in normalized
            if isinstance(item.get("write_id"), str)
        ]
        return {
            "member": member,
            "reachable": True,
            "exists": isinstance(document, dict),
            "observation_valid": observation_valid,
            "updates": normalized,
            "observed_versions": versions,
            "observed_write_ids": write_ids,
            "observed_version": max(versions) if versions else None,
        }

    def observe_document(
        self,
        member: str,
        database_name: str,
        collection_name: str,
        document_id: str,
    ) -> dict[str, Any]:
        """Read one key through a direct member connection without a subject session."""

        observed_at_ns = time.monotonic_ns()
        try:
            pymongo = require_pymongo()
            collection = self._client(member).get_database(
                database_name,
                read_concern=pymongo.read_concern.ReadConcern("local"),
            )[collection_name]
            document = collection.find_one({"_id": document_id})
            result = self._document_observation(member, document)
        except Exception as error:  # noqa: BLE001 - observer failure remains explicit.
            result = {
                "member": member,
                "reachable": False,
                "exists": False,
                "observation_valid": False,
                "updates": [],
                "observed_versions": [],
                "observed_write_ids": [],
                "observed_version": None,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        result["observed_at_ns"] = observed_at_ns
        return result

    def observe_all(
        self,
        database_name: str,
        collection_name: str,
        document_id: str,
    ) -> dict[str, dict[str, Any]]:
        return {
            member: self.observe_document(member, database_name, collection_name, document_id)
            for member in self.members
        }

    def wait_for_document_convergence(
        self,
        database_name: str,
        collection_name: str,
        document_id: str,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Wait until independent direct reads agree on the complete update list."""

        deadline = time.monotonic() + timeout_seconds
        last_members: dict[str, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            last_members = self.observe_all(database_name, collection_name, document_id)
            healthy = all(value.get("reachable") for value in last_members.values())
            valid_documents = all(
                value.get("exists") is True and value.get("observation_valid") is True
                for value in last_members.values()
            )
            documents = [value.get("updates") for value in last_members.values()]
            if (
                healthy
                and valid_documents
                and len(last_members) == len(self.members)
                and documents
                and all(value == documents[0] for value in documents[1:])
            ):
                return {
                    "members": last_members,
                    "converged": True,
                    "observed_at_ns": time.monotonic_ns(),
                }
            time.sleep(0.2)
        return {
            "members": last_members or self.observe_all(database_name, collection_name, document_id),
            "converged": False,
            "observed_at_ns": time.monotonic_ns(),
            "error": f"members did not converge within {timeout_seconds:.1f}s",
        }

    def setup_write(
        self,
        member: str,
        database_name: str,
        collection_name: str,
        document_id: str,
        update: dict[str, Any],
        *,
        write_concern: str = "majority",
        replace: bool = False,
    ) -> dict[str, Any]:
        """Issue a diagnostic/setup write without using the subject session."""

        pymongo = require_pymongo()
        collection = self._setup_client(member).get_database(
            database_name,
            write_concern=pymongo.write_concern.WriteConcern(
                w=1 if write_concern == "w:1" else "majority",
                wtimeout=SETUP_WRITE_CONCERN_TIMEOUT_MS,
            ),
        )[collection_name]
        if replace:
            result = collection.replace_one(
                {"_id": document_id},
                {"_id": document_id, "updates": [dict(update)]},
                upsert=True,
            )
            return {"matched_count": result.matched_count, "upserted_id": result.upserted_id}
        result = collection.update_one(
            {"_id": document_id},
            {"$push": {"updates": dict(update)}},
            upsert=False,
        )
        return {"matched_count": result.matched_count, "modified_count": result.modified_count}

    def close(self) -> None:
        for client in (*self._clients.values(), *self._setup_clients.values()):
            client.close()
        self._clients.clear()
        self._setup_clients.clear()

    def __enter__(self) -> TopologyOracle:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()
