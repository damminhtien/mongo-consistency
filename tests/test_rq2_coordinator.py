from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_rq2_coordinator import FaultCoordinator


class RQ2CoordinatorTests(unittest.TestCase):
    def test_controller_requests_use_sidecar_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = FaultCoordinator(Path(directory) / "active-faults.json")
            response = subprocess.CompletedProcess(
                ["docker", "compose"],
                0,
                json.dumps({"member": "mongo3", "ok": True}) + "\n",
                "",
            )
            with patch.object(coordinator, "_compose", return_value=response) as compose:
                result = coordinator._controller_request(
                    "mongo3",
                    request_kind="apply",
                    action="heal",
                    event_id="rq2-f3-r01-fault",
                )

        self.assertEqual("mongo3", result["member"])
        command = compose.call_args.args
        self.assertEqual(
            ["exec", "-T", "fault-controller-3", "python", "-c"],
            list(command[:5]),
        )
        self.assertIn("127.0.0.1", command[5])
        self.assertEqual(
            ("29093", "apply", "heal", "rq2-f3-r01-fault"),
            tuple(command[6:]),
        )


if __name__ == "__main__":
    unittest.main()
