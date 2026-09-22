from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import analyse_rq3

from mongo_consistency.config import load_configurations
from mongo_consistency.rq3 import pair_control

FIXTURE_PATH = ROOT / "tests/fixtures/rq3/protocol-v2-pairs.json"
CONFIGURATIONS = load_configurations(ROOT / "configs/configurations.json")


def _fixture_pairs() -> dict[str, dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["pairs"]


class RQ3AnalysisTests(unittest.TestCase):
    def test_m1_reports_stale_read_and_causal_bound_as_recorded(self) -> None:
        pair = _fixture_pairs()["M1"]
        row = analyse_rq3._row_for_pair("M1", pair["pair_id"], pair["histories"], CONFIGURATIONS)
        counts = analyse_rq3._counts([row], "M1")

        self.assertTrue(row["control_valid"])
        self.assertEqual(1, counts["C5_stale_success_without_after_cluster_time"])
        self.assertEqual(1, counts["C5_write_time_ahead_of_routed_member_last_write"])
        self.assertEqual(1, counts["C6_after_cluster_time_matches_write_time"])
        self.assertEqual(1, counts["C6_after_cluster_time_ahead_of_routed_member_last_write"])
        self.assertEqual(1, counts["C6_unavailable_reads"])

    def test_m2_setup_write_event_proves_w1_route(self) -> None:
        pair = _fixture_pairs()["M2"]
        control = pair_control("M2", pair["pair_id"], pair["histories"], configurations=CONFIGURATIONS)

        setup = control["observed"]["C8"]["observed"]["setup_write"]
        self.assertTrue(control["control_valid"])
        self.assertEqual("mongo3", setup["member"])
        self.assertEqual({"w": 1}, setup["write_concern"])
        self.assertTrue(setup["command_started"])

    def test_m3_keeps_acknowledgement_timeout_and_final_presence_distinct(self) -> None:
        pair = _fixture_pairs()["M3"]
        row = analyse_rq3._row_for_pair("M3", pair["pair_id"], pair["histories"], CONFIGURATIONS)
        counts = analyse_rq3._counts([row], "M3")

        self.assertTrue(row["control_valid"])
        self.assertEqual(1, counts["C3_w1_first_write_acknowledged"])
        self.assertEqual(1, counts["C3_acknowledged_w1_absent_from_all_converged_members"])
        self.assertEqual(1, counts["C6_majority_first_write_network_timeout"])
        self.assertEqual(1, counts["C6_w1_present_on_all_final_members_after_timeout"])

    def test_consistency_outcomes_do_not_change_control_validity(self) -> None:
        pair = copy.deepcopy(_fixture_pairs()["M2"])
        histories = pair["histories"]
        histories["C8"]["result"] = "VIOLATION"
        histories["C5"]["result"] = "PASS"

        control = pair_control("M2", pair["pair_id"], histories, configurations=CONFIGURATIONS)
        self.assertTrue(control["control_valid"])

        histories["C8"]["operations"][0]["actual_server_address"] = "mongo2:27017"
        invalid = pair_control("M2", pair["pair_id"], histories, configurations=CONFIGURATIONS)
        self.assertFalse(invalid["control_valid"])
        self.assertIn("C8: read route is 'mongo2', expected mongo3", invalid["invalid_reasons"])

    def test_analyzer_source_contains_no_report_writer(self) -> None:
        source = (ROOT / "scripts/analyse_rq3.py").read_text(encoding="utf-8")
        self.assertNotIn("generated-rq3.tex", source)
        self.assertNotIn("pdflatex", source)
        self.assertNotIn("submission_root", source)
        self.assertEqual(2, source.count(".write_text("))


if __name__ == "__main__":
    unittest.main()
