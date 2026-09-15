from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.config import load_configurations, load_json
from scripts.run_campaign import adversarial_cases, campaign_cases


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


if __name__ == "__main__":
    unittest.main()
