from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from mongo_consistency.topology import TopologyError, TopologyOracle


class FakeAdmin:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.client.commands.append(dict(command))
        if "hello" in command:
            self.client.hello_count += 1
            state = self.client.oracle_test_state
            if state.get("unreachable"):
                raise OSError("member is unreachable")
            return {
                "setName": state.get("set_name", "rs0"),
                "me": f"{self.client.member}:27017",
                "isWritablePrimary": state["role"] == "PRIMARY",
                "secondary": state["role"] == "SECONDARY",
                "electionId": state.get("election_id", "election-1"),
                "setVersion": state.get("set_version", 4),
                "lastWrite": {
                    "opTime": {
                        "ts": state.get(
                            "last_write_op_time", {"t": 20, "i": 7}
                        )
                    }
                },
            }
        if "replSetGetStatus" in command:
            return {
                "optimes": {
                    "lastCommittedOpTime": {"ts": {"t": 20, "i": 9}}
                },
                "members": [
                    {
                        "name": f"{self.client.member}:27017",
                        "syncSourceHost": self.client.oracle_test_state.get(
                            "sync_source", ""
                        ),
                    }
                ],
            }
        if "replSetSyncFrom" in command:
            previous = self.client.oracle_test_state.get("sync_source", "")
            requested = command["replSetSyncFrom"]
            self.client.oracle_test_state["sync_source"] = requested
            return {
                "ok": 1,
                "syncFromRequested": requested,
                "prevSyncTarget": previous,
            }
        if "replSetFreeze" in command or "replSetStepDown" in command:
            return {"ok": 1}
        raise AssertionError(f"unexpected admin command: {command}")


class FakeCollection:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def find_one(self, _query: dict[str, Any]) -> dict[str, Any] | None:
        document = self.client.documents.get(self.client.member)
        return dict(document) if isinstance(document, dict) else document

    @staticmethod
    def update_one(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(matched_count=1, modified_count=1)

    @staticmethod
    def replace_one(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(matched_count=0, upserted_id="x")


class FakeDatabase:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def __getitem__(self, _collection: str) -> FakeCollection:
        return FakeCollection(self.client)


class FakeClient:
    def __init__(
        self,
        member: str,
        state: dict[str, Any],
        documents: dict[str, dict[str, Any] | None],
        options: dict[str, Any],
    ) -> None:
        self.member = member
        self.oracle_test_state = state
        self.documents = documents
        self.options = options
        self.database_options: dict[str, Any] = {}
        self.commands: list[dict[str, Any]] = []
        self.admin = FakeAdmin(self)
        self.hello_count = 0
        self.closed = False

    def get_database(self, _name: str, **options: Any) -> FakeDatabase:
        self.database_options = options
        return FakeDatabase(self)

    def close(self) -> None:
        self.closed = True


class TopologyOracleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.members = {
            "mongo1": "mongodb://mongo1:27017/",
            "mongo2": "mongodb://mongo2:27017/",
            "mongo3": "mongodb://mongo3:27017/",
        }
        self.states: dict[str, dict[str, Any]] = {
            "mongo1": {"role": "PRIMARY"},
            "mongo2": {"role": "SECONDARY"},
            "mongo3": {"role": "SECONDARY"},
        }
        update = {"write_id": "init", "version": 0}
        self.documents: dict[str, dict[str, Any] | None] = {
            member: {"_id": "x", "updates": [dict(update)]}
            for member in self.members
        }
        self.clients: dict[str, FakeClient] = {}
        self.created_clients: list[FakeClient] = []

        def factory(uri: str, **options: Any) -> FakeClient:
            member = uri.split("//", 1)[1].split(":", 1)[0]
            client = FakeClient(member, self.states[member], self.documents, options)
            self.clients[member] = client
            self.created_clients.append(client)
            return client

        self.oracle = TopologyOracle(self.members, client_factory=factory)

    def tearDown(self) -> None:
        self.oracle.close()

    def test_snapshot_requires_three_reachable_members_with_one_primary(self) -> None:
        state = self.oracle.snapshot()

        self.assertTrue(state.stable)
        self.assertEqual("mongo1", state.primary)
        self.assertEqual(("mongo2", "mongo3"), state.secondaries)
        self.assertEqual({"seconds": 20, "increment": 9}, state.last_committed_op_time)
        self.assertTrue(all(client.options["directConnection"] for client in self.clients.values()))
        self.assertTrue(all(client.options["retryReads"] is False for client in self.clients.values()))
        self.assertTrue(all(client.options["retryWrites"] is False for client in self.clients.values()))

    def test_non_replica_member_makes_topology_unstable(self) -> None:
        self.states["mongo3"]["set_name"] = "other-rs"

        state = self.oracle.snapshot()

        self.assertFalse(state.stable)
        self.assertFalse(state.members["mongo3"]["reachable"])
        with self.assertRaises(TopologyError):
            self.oracle.wait_for_stable(timeout_seconds=0.01)

    def test_majority_election_observation_excludes_isolated_primary(self) -> None:
        self.states["mongo1"]["role"] = "UNREACHABLE"
        self.states["mongo1"]["unreachable"] = True
        self.states["mongo2"]["role"] = "PRIMARY"

        new_primary = self.oracle.wait_for_majority_primary(
            "mongo1",
            timeout_seconds=0.1,
            stable_samples=1,
        )

        self.assertEqual("mongo2", new_primary)
        self.assertEqual(0, self.clients.get("mongo1", FakeClient("x", {}, {}, {})).hello_count)

    def test_data_convergence_requires_equal_optimes_across_all_members(self) -> None:
        result = self.oracle.wait_for_data_convergence(
            timeout_seconds=0.1,
            stable_samples=1,
        )

        self.assertTrue(result["stable"])
        self.assertEqual(1, result["stable_samples"])
        self.states["mongo3"]["last_write_op_time"] = {"t": 30, "i": 1}
        with self.assertRaisesRegex(TopologyError, "did not converge"):
            self.oracle.wait_for_data_convergence(
                timeout_seconds=0.1,
                stable_samples=1,
            )

    def test_election_controls_use_direct_admin_commands(self) -> None:
        self.oracle.freeze_member("mongo2", 120)
        self.oracle.freeze_member("mongo2", 0)
        self.oracle.step_down_primary("mongo1")

        self.assertEqual(
            [
                {"replSetFreeze": 120},
                {"replSetFreeze": 0},
            ],
            self.clients["mongo2"].commands,
        )
        self.assertEqual(
            {
                "replSetStepDown": 60,
                "secondaryCatchUpPeriodSecs": 10,
                "force": False,
            },
            self.clients["mongo1"].commands[-1],
        )

    def test_sync_source_can_be_observed_and_temporarily_overridden(self) -> None:
        self.states["mongo3"]["sync_source"] = "mongo2:27017"

        self.assertEqual("mongo2:27017", self.oracle.sync_source("mongo3"))
        reply = self.oracle.sync_from("mongo3", "mongo1:27017")
        source = self.oracle.wait_for_sync_source(
            "mongo3",
            "mongo1:27017",
            timeout_seconds=0.1,
        )

        self.assertEqual("mongo2:27017", reply["prevSyncTarget"])
        self.assertEqual("mongo1:27017", source)
        self.assertIn(
            {"replSetSyncFrom": "mongo1:27017"},
            self.clients["mongo3"].commands,
        )

    def test_independent_observer_waits_for_all_three_document_copies(self) -> None:
        pymongo_stub = SimpleNamespace(
            read_concern=SimpleNamespace(ReadConcern=lambda level: level),
            write_concern=SimpleNamespace(WriteConcern=lambda **options: options),
        )
        with patch("mongo_consistency.topology.require_pymongo", return_value=pymongo_stub):
            result = self.oracle.wait_for_document_convergence("db", "items", "x", 0.1)

        self.assertTrue(result["converged"])
        self.assertEqual({"mongo1", "mongo2", "mongo3"}, set(result["members"]))
        self.assertTrue(all(item["observation_valid"] for item in result["members"].values()))

    def test_setup_write_uses_socket_deadline_after_write_concern_deadline(self) -> None:
        pymongo_stub = SimpleNamespace(
            write_concern=SimpleNamespace(WriteConcern=lambda **options: options),
        )
        self.oracle.member_state("mongo1")
        with patch("mongo_consistency.topology.require_pymongo", return_value=pymongo_stub):
            result = self.oracle.setup_write(
                "mongo1",
                "db",
                "items",
                "x",
                {"write_id": "prep", "version": 1},
            )

        self.assertEqual({"matched_count": 1, "modified_count": 1}, result)
        self.assertEqual(2, len(self.created_clients))
        self.assertIsNot(self.created_clients[0], self.created_clients[1])
        self.assertEqual(2000, self.created_clients[0].options["socketTimeoutMS"])
        self.assertEqual(17000, self.created_clients[1].options["socketTimeoutMS"])
        self.assertEqual(
            15000,
            self.created_clients[1].database_options["write_concern"]["wtimeout"],
        )

    def test_close_closes_diagnostic_and_setup_clients(self) -> None:
        pymongo_stub = SimpleNamespace(
            write_concern=SimpleNamespace(WriteConcern=lambda **options: options),
        )
        self.oracle.member_state("mongo1")
        with patch("mongo_consistency.topology.require_pymongo", return_value=pymongo_stub):
            self.oracle.setup_write(
                "mongo2",
                "db",
                "items",
                "x",
                {"write_id": "prep", "version": 1},
            )

        self.oracle.close()

        self.assertTrue(all(client.closed for client in self.created_clients))
        self.assertEqual({}, self.oracle._clients)
        self.assertEqual({}, self.oracle._setup_clients)

    def test_missing_documents_never_count_as_converged(self) -> None:
        self.documents = {member: None for member in self.members}
        for client in self.clients.values():
            client.documents = self.documents
        pymongo_stub = SimpleNamespace(
            read_concern=SimpleNamespace(ReadConcern=lambda level: level),
            write_concern=SimpleNamespace(WriteConcern=lambda **options: options),
        )
        with patch("mongo_consistency.topology.require_pymongo", return_value=pymongo_stub):
            result = self.oracle.wait_for_document_convergence("db", "items", "missing", 0.01)

        self.assertFalse(result["converged"])
        self.assertTrue(all(item["exists"] is False for item in result["members"].values()))


if __name__ == "__main__":
    unittest.main()
