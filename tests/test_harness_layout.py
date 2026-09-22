from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.driver import RoutingMonitor, operation_record_from_events
from mongo_consistency.models import OperationRecord
from scripts.setup_experiment import (
    committed_file_hash,
    committed_file_revision,
    initialize_replica_set,
    working_tree_clean,
)


class HarnessLayoutTests(unittest.TestCase):
    def test_provenance_clean_check_scopes_to_runtime_inputs(self) -> None:
        for status_output, expected in (
            ("", True),
            (" M src/package.py\n", False),
        ):
            completed = subprocess.CompletedProcess(
                ["git", "status"], 0, status_output, ""
            )
            with patch("scripts.setup_experiment.run", return_value=completed) as run_mock:
                self.assertEqual(expected, working_tree_clean())

            command = run_mock.call_args.args[0]
            self.assertIn("--untracked-files=all", command)
            for path in ("Makefile", "compose.yaml", "configs/", "infra/", "scripts/", "src/"):
                self.assertIn(path, command)
            self.assertNotIn("README.md", command)

    def test_setup_captures_replica_status_through_temporary_output_mount(self) -> None:
        def fake_run(
            command: list[str],
            *,
            env: dict[str, str] | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            self.assertIsNotNone(env)
            capture_dir = Path(env["MC_RESULTS_MOUNT"])
            (capture_dir / "replica-status.json").write_text(
                '{"replica_set":"rs0","members":[]}',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("scripts.setup_experiment.run", side_effect=fake_run) as run_mock:
            payload = initialize_replica_set()

        self.assertEqual("rs0", payload["replica_set"])
        command = run_mock.call_args.args[0]
        self.assertEqual(
            "/workspace/results/raw/replica-status.json",
            command[command.index("--output") + 1],
        )
        capture_dir = Path(run_mock.call_args.kwargs["env"]["MC_RESULTS_MOUNT"])
        self.assertFalse(capture_dir.exists(), "temporary setup mount should be removed")

    def test_frozen_protocol_provenance_uses_committed_content(self) -> None:
        responses = [
            subprocess.CompletedProcess(["git", "log"], 0, "frozen-commit\n", ""),
            subprocess.CompletedProcess(["git", "show"], 0, "committed protocol\n", ""),
        ]
        with patch("scripts.setup_experiment.run", side_effect=responses) as run_mock:
            revision = committed_file_revision("docs/experimental-protocol.md")
            digest = committed_file_hash("docs/experimental-protocol.md")

        self.assertEqual("frozen-commit", revision)
        self.assertEqual(
            hashlib.sha256(b"committed protocol\n").hexdigest(),
            digest,
        )
        self.assertEqual("log", run_mock.call_args_list[0].args[0][1])
        self.assertEqual("show", run_mock.call_args_list[1].args[0][1])

    def test_compose_has_three_members_and_separate_networks(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        for member in ("mongo1", "mongo2", "mongo3"):
            self.assertIn(f"  {member}:", compose)
        self.assertIn("client_net", compose)
        self.assertIn("replica_net", compose)
        self.assertIn("ipv4_address: ${MC_MONGO1_REPLICA_IP:-172.20.0.2}", compose)
        self.assertIn('"mongo1=${MC_MONGO1_REPLICA_IP:-172.20.0.2}"', compose)
        self.assertIn("subnet: ${MC_REPLICA_SUBNET:-172.20.0.0/16}", compose)
        self.assertIn("  runner:", compose)
        self.assertIn("image: mongo-consistency-runner:local", compose)
        runner_section = compose.split("  runner:", 1)[1]
        self.assertIn("    networks: [client_net]", runner_section)
        self.assertIn("${MC_RESULTS_MOUNT:-./results/raw}:/workspace/results/raw:rw", runner_section)
        self.assertNotIn("${MC_RESULTS_MOUNT:-./results}:/workspace/results:rw", runner_section)
        self.assertNotIn("/var/run/docker.sock", runner_section)
        self.assertNotIn("docker.sock", compose)

    def test_compose_and_runner_use_locked_versions(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "infra/runner/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("mongo:7.0.34", compose)
        self.assertIn("python:3.14.7-slim-bookworm", dockerfile)
        self.assertIn("pymongo==4.18.1", (ROOT / "requirements.txt").read_text())

    def test_runner_dockerfile_copies_only_runtime_inputs(self) -> None:
        dockerfile = (ROOT / "infra/runner/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY src /workspace/src", dockerfile)
        self.assertIn("COPY scripts /workspace/scripts", dockerfile)
        self.assertNotIn("COPY .", dockerfile)

    def test_fault_and_cleanup_contracts_are_present(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        workloads = (ROOT / "src/mongo_consistency/workloads.py").read_text(encoding="utf-8")
        topology = (ROOT / "src/mongo_consistency/topology.py").read_text(encoding="utf-8")
        trial = (ROOT / "src/mongo_consistency/trial.py").read_text(encoding="utf-8")
        controller = (ROOT / "infra/fault-controller/server.py").read_text(encoding="utf-8")
        self.assertIn("election_barrier_ms", trial)
        self.assertIn("wait_for_stable", topology)
        self.assertIn("wait_for_stable", workloads)
        self.assertIn("wait_for_document_convergence", topology)
        self.assertIn('"cleanup_status"', workloads)
        self.assertIn("interface_for_subnet", controller)
        self.assertIn('os.environ.get("REPLICA_INTERFACE") or interface_for_subnet', controller)
        for member in (1, 2, 3):
            section = compose.split(f"  fault-controller-{member}:\n", 1)[1]
            if member < 3:
                section = section.split("\n  fault-controller-", 1)[0]
            self.assertIn(
                "REPLICA_SUBNET: ${MC_REPLICA_SUBNET:-172.20.0.0/16}",
                section,
            )
        self.assertNotIn("REPLICA_INTERFACE:", compose)
        self.assertIn('iptables("-A", CHAIN, "-j", "DROP")', controller)
        self.assertIn('add_jump("INPUT")', controller)
        self.assertIn('add_jump("OUTPUT")', controller)
        self.assertIn('remove_jump("OUTPUT")', controller)
        self.assertIn('return [interface_flag, INTERFACE, "-j", CHAIN]', controller)
        self.assertNotIn('"--sport"', controller)
        self.assertNotIn('"--dport"', controller)
        self.assertIn("stale, fresh = initial.secondaries", workloads)
        self.assertNotIn('"REJECT"', controller)
        self.assertNotIn("docker.sock", compose)

    def test_topology_oracle_has_no_assumed_secondary_fallback(self) -> None:
        topology = (ROOT / "src/mongo_consistency/topology.py").read_text(encoding="utf-8")
        workloads = (ROOT / "src/mongo_consistency/workloads.py").read_text(encoding="utf-8")
        self.assertIn('"hello": 1', topology)
        self.assertIn("len(secondaries) == 2", topology)
        self.assertNotIn('return ("mongo2", "mongo3")', workloads)

    def test_runner_waits_for_healthy_fault_controllers(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        runner_section = compose.split("  runner:", 1)[1].split("\n  fault-controller-1:", 1)[0]
        for member, port in ((1, 29091), (2, 29092), (3, 29093)):
            self.assertIn(
                f"fault-controller-{member}: {{condition: service_healthy}}",
                runner_section,
            )
            section = compose.split(f"  fault-controller-{member}:\n", 1)[1]
            if member < 3:
                section = section.split("\n  fault-controller-", 1)[0]
            self.assertIn("image: mongo-consistency-fault-controller:local", section)
            self.assertIn("healthcheck:", section)
            self.assertIn(f"http://127.0.0.1:{port}/health", section)

    def test_driver_uses_pymongo_write_concern_timeout_name(self) -> None:
        driver = (ROOT / "src/mongo_consistency/driver.py").read_text(encoding="utf-8")
        self.assertIn("wtimeout=5000", driver)
        self.assertNotIn("wtimeoutMS", driver)

    def test_setup_records_runtime_kernel_for_reproducibility(self) -> None:
        setup = (ROOT / "scripts/setup_experiment.py").read_text(encoding="utf-8")
        self.assertIn("KernelVersion", setup)

    def test_command_monitor_preserves_actual_route(self) -> None:
        class Event:
            request_id = 17
            command_name = "find"
            connection_id = ("mongo2", 27017)
            failure = None

        monitor = RoutingMonitor(lambda address: "RSSecondary" if address == "mongo2:27017" else None)
        monitor.attach("read")
        monitor.started(Event())
        monitor.succeeded(Event())
        operation = OperationRecord(operation_id="read", kind="read", key="x")
        operation_record_from_events(operation, monitor.events_for("read"))
        self.assertEqual("mongo2:27017", operation.actual_server_address)
        self.assertIsNone(operation.actual_role)
        self.assertEqual("RSSecondary", operation.driver_reported_role)
        self.assertEqual("find", operation.command_name)

    def test_command_monitor_adapter_implements_listener_base(self) -> None:
        class ListenerBase:
            pass

        class Event:
            request_id = 23
            command_name = "ping"
            connection_id = ("mongo1", 27017)
            failure = None

        monitor = RoutingMonitor()
        listener = monitor.command_listener(ListenerBase)
        self.assertIsInstance(listener, ListenerBase)
        monitor.attach("ping")
        listener.started(Event())
        listener.succeeded(Event())
        self.assertEqual("ping", monitor.events_for("ping")[0]["command_name"])

    def test_secondary_routing_uses_tagged_constructor(self) -> None:
        driver = (ROOT / "src/mongo_consistency/driver.py").read_text(encoding="utf-8")
        self.assertIn("pymongo.read_preferences.Secondary(", driver)
        self.assertIn('tag_sets=[{"member": member_tag}]', driver)
        self.assertNotIn("ReadPreference.SECONDARY.with_options", driver)

    def test_missing_document_is_recorded_as_a_successful_empty_read(self) -> None:
        trial = (ROOT / "src/mongo_consistency/trial.py").read_text(encoding="utf-8")
        self.assertIn("if document is None:", trial)
        self.assertIn("operation.observed_updates = ()", trial)
        self.assertNotIn('if not document or not isinstance(document.get("updates"), list):', trial)


if __name__ == "__main__":
    unittest.main()
