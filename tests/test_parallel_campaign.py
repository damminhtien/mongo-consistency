from __future__ import annotations

import ipaddress
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.history import write_history
from mongo_consistency.models import History
from scripts.run_campaign import (
    campaign_plan,
    load_configurations,
    load_json,
    trial_id_for,
)
from scripts.run_parallel_campaign import (
    WorkerSpec,
    _merge_worker_histories,
    _prepare_worker,
    _validate_history_directory,
    worker_environment,
    worker_resources,
)


class ParallelCampaignTests(unittest.TestCase):
    def _small_plan(self) -> list[tuple[int, str, str, bool]]:
        return campaign_plan(
            "experiment",
            load_configurations(ROOT / "configs/configurations.json"),
            load_json(ROOT / "configs/campaign.json"),
        )[:4]

    def _write_fixture(
        self,
        path: Path,
        case: tuple[int, str, str, bool],
    ) -> None:
        ordinal, configuration_id, property_name, adversarial = case
        trial_id = trial_id_for("experiment", ordinal, configuration_id, property_name)
        write_history(
            path,
            History(
                manifest={
                    "trial_id": trial_id,
                    "campaign_id": "experiment",
                    "configuration_id": configuration_id,
                    "property": property_name,
                    "adversarial": adversarial,
                    "seed": 20260915 + ordinal,
                },
                operations=[],
            ),
        )

    def _temporary_spec(self, root: Path, index: int) -> WorkerSpec:
        return WorkerSpec(
            campaign="experiment",
            index=index,
            count=2,
            project_name=f"test-worker-{index}",
            client_subnet=ipaddress.ip_network(f"172.{24 + index * 2}.0.0/24"),
            replica_subnet=ipaddress.ip_network(f"172.{25 + index * 2}.0.0/24"),
            host_ports=tuple(28100 + index * 10 + offset for offset in range(6)),
            results_mount=root / f"worker-{index}" / "raw",
            figures_mount=root / f"worker-{index}" / "figures",
        )

    def test_worker_resources_are_unique_and_fixed(self) -> None:
        specs = [
            worker_resources(
                "experiment",
                index,
                2,
                ipaddress.ip_network(f"172.{24 + index * 2}.0.0/24"),
                ipaddress.ip_network(f"172.{25 + index * 2}.0.0/24"),
                28000 + index * 20,
            )
            for index in range(2)
        ]
        self.assertEqual(
            ["mc-experiment-w02-00", "mc-experiment-w02-01"],
            [spec.project_name for spec in specs],
        )
        self.assertEqual(
            ["172.24.0.0/24", "172.26.0.0/24"],
            [str(spec.client_subnet) for spec in specs],
        )
        self.assertEqual(
            ["172.25.0.0/24", "172.27.0.0/24"],
            [str(spec.replica_subnet) for spec in specs],
        )
        self.assertNotEqual(specs[0].host_ports, specs[1].host_ports)
        self.assertNotEqual(specs[0].results_mount, specs[1].results_mount)

    def test_worker_environment_does_not_share_result_mounts(self) -> None:
        spec = worker_resources(
            "experiment",
            1,
            2,
            ipaddress.ip_network("172.26.0.0/24"),
            ipaddress.ip_network("172.27.0.0/24"),
            28020,
        )
        environment = worker_environment(spec)
        self.assertEqual("172.26.0.0/24", environment["MC_CLIENT_SUBNET"])
        self.assertEqual("172.27.0.0/24", environment["MC_REPLICA_SUBNET"])
        self.assertEqual("172.26.0.2", environment["MC_MONGO1_CLIENT_IP"])
        self.assertEqual("172.27.0.4", environment["MC_MONGO3_REPLICA_IP"])
        self.assertEqual("28020", environment["MC_MONGO1_HOST_PORT"])
        self.assertEqual(str(spec.results_mount), environment["MC_RESULTS_MOUNT"])
        self.assertNotEqual(environment["MC_RESULTS_MOUNT"], str(ROOT / "results"))

    def test_prepare_copies_only_assigned_canonical_histories(self) -> None:
        plan = self._small_plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_dir = root / "canonical" / "experiment"
            first_trial_id = trial_id_for("experiment", plan[0][0], plan[0][1], plan[0][2])
            self._write_fixture(canonical_dir / f"{first_trial_id}.json", plan[0])
            canonical_records = _validate_history_directory(
                canonical_dir,
                campaign="experiment",
                plan=plan,
                seed_base=20260915,
            )
            spec = self._temporary_spec(root, 0)
            worker_records = _prepare_worker(
                spec,
                campaign="experiment",
                plan=plan,
                seed_base=20260915,
                canonical_dir=canonical_dir,
                canonical_records=canonical_records,
            )
            self.assertEqual([1], sorted(worker_records))
            worker_path = spec.results_mount / "experiment" / f"{first_trial_id}.json"
            self.assertEqual(
                (canonical_dir / f"{first_trial_id}.json").read_bytes(),
                worker_path.read_bytes(),
            )
            self.assertFalse(
                (spec.results_mount / "experiment" / f"{trial_id_for('experiment', 2, plan[1][1], plan[1][2])}.json").exists()
            )

    def test_merge_copies_worker_history_and_normalizes_canonical_path(self) -> None:
        plan = self._small_plan()
        case = plan[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._temporary_spec(root, 1)
            worker_dir = spec.results_mount / "experiment"
            trial_id = trial_id_for("experiment", case[0], case[1], case[2])
            worker_path = worker_dir / f"{trial_id}.json"
            self._write_fixture(worker_path, case)
            canonical_dir = root / "canonical" / "experiment"
            merged = _merge_worker_histories(
                campaign="experiment",
                plan=plan,
                seed_base=20260915,
                canonical_dir=canonical_dir,
                canonical_records={},
                worker_specs=[spec],
            )
            canonical_path = canonical_dir / worker_path.name
            self.assertTrue(canonical_path.is_file())
            self.assertEqual(canonical_path.as_posix(), merged[case[0]]["path"])
            self.assertEqual(worker_path.read_bytes(), canonical_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
