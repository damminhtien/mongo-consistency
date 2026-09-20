from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.config import load_configurations, load_json
from scripts.run_campaign import adversarial_cases, campaign_cases, campaign_plan


class CampaignPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configurations = load_configurations(ROOT / "configs/configurations.json")
        self.campaign = load_json(ROOT / "configs/campaign.json")

    def test_campaign_counts_match_locked_design(self) -> None:
        self.assertEqual(320, len(campaign_cases("normal", self.configurations, self.campaign)))
        self.assertEqual(32, len(campaign_cases("pilot", self.configurations, self.campaign)))
        self.assertEqual(160, len(adversarial_cases("pilot", self.configurations, self.campaign)))
        self.assertEqual(320, len(campaign_cases("experiment", self.configurations, self.campaign)))
        self.assertEqual(960, len(adversarial_cases("experiment", self.configurations, self.campaign)))

    def test_compose_starts_all_controller_sidecars(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        for number in (1, 2, 3):
            self.assertIn(f"fault-controller-{number}:", compose)
        self.assertEqual(3, compose.count("cap_add: [NET_ADMIN]"))

    def test_shards_partition_the_global_plan_without_reordering_it(self) -> None:
        plan = campaign_plan("experiment", self.configurations, self.campaign)
        shards = [
            [case for case in plan if (case[0] - 1) % 4 == shard_index]
            for shard_index in range(4)
        ]
        self.assertEqual(list(range(1, 1281)), [case[0] for case in plan])
        self.assertEqual([320, 320, 320, 320], [len(shard) for shard in shards])
        self.assertEqual(
            list(range(1, 1281)),
            sorted(case[0] for shard in shards for case in shard),
        )

    def test_property_rerun_keeps_original_ordinals_and_case_identity(self) -> None:
        full = campaign_plan("experiment", self.configurations, self.campaign)
        monotonic_reads = campaign_plan(
            "experiment",
            self.configurations,
            self.campaign,
            property_filter="MR",
        )

        self.assertEqual(320, len(monotonic_reads))
        self.assertEqual(
            [case for case in full if case[2] == "MR"],
            monotonic_reads,
        )
        self.assertEqual(80, sum(not case[3] for case in monotonic_reads))
        self.assertEqual(240, sum(case[3] for case in monotonic_reads))

    def test_property_rerun_rejects_unknown_property(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown property filter"):
            campaign_plan(
                "experiment",
                self.configurations,
                self.campaign,
                property_filter="UNKNOWN",
            )


if __name__ == "__main__":
    unittest.main()
