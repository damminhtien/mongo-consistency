from __future__ import annotations

import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
