from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from scripts import check_runner_isolation


class RunnerIsolationTests(unittest.TestCase):
    def _service(self, *, volumes=None, environment=None) -> dict[str, object]:
        return {
            "services": {
                "runner": {
                    "volumes": volumes
                    or [
                        {
                            "type": "bind",
                            "source": path.as_posix(),
                            "target": target,
                        }
                        for path, target in (
                            (
                                check_runner_isolation.ROOT / "results/raw",
                                "/workspace/results/raw",
                            ),
                            (
                                check_runner_isolation.ROOT / "figures",
                                "/workspace/figures",
                            ),
                            (
                                check_runner_isolation.ROOT / "results/setup/toolchain.json",
                                "/workspace/setup/toolchain.json",
                            ),
                        )
                    ],
                    "environment": environment or {},
                    "networks": {"client_net": None},
                    "read_only": True,
                    "cap_drop": ["ALL"],
                }
            }
        }

    def test_static_probe_accepts_whitelisted_mounts(self) -> None:
        completed = subprocess.CompletedProcess(
            ["docker", "compose"],
            0,
            json.dumps(self._service()),
            "",
        )
        with patch("scripts.check_runner_isolation.run", return_value=completed):
            result = check_runner_isolation._config_probe()
        self.assertEqual(
            sorted(check_runner_isolation.ALLOWED_TARGETS), result["mount_targets"]
        )
        self.assertEqual([], result["forbidden_environment_names"])

    def test_static_probe_rejects_socket_mount(self) -> None:
        volumes = [
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock",
            }
        ]
        completed = subprocess.CompletedProcess(
            ["docker", "compose"],
            0,
            json.dumps(self._service(volumes=volumes)),
            "",
        )
        with patch("scripts.check_runner_isolation.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "Docker socket"):
                check_runner_isolation._config_probe()

    def test_static_probe_rejects_credential_environment(self) -> None:
        completed = subprocess.CompletedProcess(
            ["docker", "compose"],
            0,
            json.dumps(self._service(environment={"DOCKER_CONFIG": "/tmp/docker"})),
            "",
        )
        with patch("scripts.check_runner_isolation.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "credential names"):
                check_runner_isolation._config_probe()


if __name__ == "__main__":
    unittest.main()
