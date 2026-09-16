from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mongo_consistency.driver import RoutingMonitor, operation_record_from_events
from mongo_consistency.models import OperationRecord

ROOT = Path(__file__).resolve().parents[1]


class HarnessLayoutTests(unittest.TestCase):
    def test_compose_has_three_members_and_separate_networks(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        for member in ("mongo1", "mongo2", "mongo3"):
            self.assertIn(f"  {member}:", compose)
        self.assertIn("client_net", compose)
        self.assertIn("replica_net", compose)
        self.assertIn("  runner:", compose)
        runner_section = compose.split("  runner:", 1)[1]
        self.assertIn("    networks: [client_net]", runner_section)
        self.assertNotIn("docker.sock", compose)

    def test_compose_and_runner_use_locked_versions(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "infra/runner/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("mongo:8.0.32", compose)
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
        controller = (ROOT / "infra/fault-controller/server.py").read_text(encoding="utf-8")
        self.assertIn("election_barrier_ms", (ROOT / "src/mongo_consistency/trial.py").read_text())
        self.assertIn("wait_for_stable_topology", workloads)
        self.assertIn('"cleanup_status"', workloads)
        self.assertIn("eth1", controller)
        self.assertNotIn("docker.sock", compose)

    def test_setup_fails_before_cluster_start_on_known_kernel_range(self) -> None:
        setup = (ROOT / "scripts/setup_experiment.py").read_text(encoding="utf-8")
        self.assertIn("kernel_tuple", setup)
        self.assertIn("7.0.14", setup)
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
        self.assertEqual("RSSecondary", operation.actual_role)
        self.assertEqual("find", operation.command_name)


if __name__ == "__main__":
    unittest.main()
