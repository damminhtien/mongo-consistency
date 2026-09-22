from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_rq3_campaign import (
    _manifest_payload,
    _pair_is_complete,
    _plan,
    _sha256,
    _validate_configuration_contrasts,
    run_campaign,
)

from mongo_consistency.config import load_configurations
from mongo_consistency.models import History
from mongo_consistency.rq3 import TOPOLOGY_PLANS, normalize_topology, pair_control
from mongo_consistency.topology import TopologyState
from run_campaign import run_case


def _state(primary: str) -> TopologyState:
    return TopologyState(
        primary=primary,
        secondaries=tuple(sorted({"mongo1", "mongo2", "mongo3"} - {primary})),
        members={
            member: {
                "member": member,
                "reachable": True,
                "role": "PRIMARY" if member == primary else "SECONDARY",
            }
            for member in ("mongo1", "mongo2", "mongo3")
        },
        stable=True,
        observed_at_ns=1,
    )


class FakeController:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.heal_calls: list[tuple[list[str], str]] = []

    def heal_many(self, members: list[str], event_id: str) -> None:
        self.heal_calls.append((members, event_id))

    def health(self) -> dict[str, dict[str, bool]]:
        return {
            member: {"replication_isolated": not self.healthy}
            for member in ("mongo1", "mongo2", "mongo3")
        }


class FakeOracle:
    def __init__(self, primary: str) -> None:
        self.members = {"mongo1": "uri1", "mongo2": "uri2", "mongo3": "uri3"}
        self.primary = primary
        self.frozen: set[str] = set()
        self.freeze_calls: list[tuple[str, int]] = []
        self.step_down_calls: list[tuple[str, int]] = []

    def wait_for_stable(self, _timeout: float) -> TopologyState:
        return _state(self.primary)

    def wait_for_data_convergence(self, _timeout: float) -> dict[str, object]:
        return {"stable": True, "stable_samples": 3, "members": {}}

    def wait_for_primary(self, expected_primary: str, _timeout: float) -> TopologyState:
        if self.primary != expected_primary:
            raise RuntimeError(f"primary is {self.primary}, expected {expected_primary}")
        return _state(self.primary)

    def freeze_member(self, member: str, seconds: int) -> dict[str, int]:
        self.freeze_calls.append((member, seconds))
        if seconds:
            self.frozen.add(member)
        else:
            self.frozen.discard(member)
        return {"ok": 1}

    def step_down_primary(self, member: str, *, seconds: int = 60) -> dict[str, int]:
        self.step_down_calls.append((member, seconds))
        if member != self.primary:
            raise RuntimeError("stepdown target is not primary")
        candidates = sorted(set(self.members) - {member} - self.frozen)
        if len(candidates) != 1:
            raise RuntimeError(f"expected one unfrozen candidate, got {candidates}")
        self.primary = candidates[0]
        return {"ok": 1}


class RQ3CampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configurations = load_configurations(ROOT / "configs/configurations.json")

    def test_configuration_contrasts_change_only_the_registered_factor(self) -> None:
        _validate_configuration_contrasts(self.configurations)
        altered = {key: dict(value) for key, value in self.configurations.items()}
        altered["C5"]["retry_reads"] = True

        with self.assertRaisesRegex(ValueError, "other configuration fields differ"):
            _validate_configuration_contrasts(altered)

    def test_plan_has_48_histories_with_stable_matched_pair_identity(self) -> None:
        cases = _plan(8, 250_000)

        self.assertEqual(48, len(cases))
        for contrast_id in ("M1", "M2", "M3"):
            contrast_cases = [case for case in cases if case.contrast.contrast_id == contrast_id]
            self.assertEqual(16, len(contrast_cases))
            for replicate in range(1, 9):
                pair = [case for case in contrast_cases if case.replicate == replicate]
                self.assertEqual(2, len(pair))
                self.assertEqual(1, len({case.pair_id for case in pair}))
                self.assertEqual(1, len({case.pair_seed for case in pair}))
                baseline = ("C5", "C6") if contrast_id == "M1" else (
                    ("C8", "C5") if contrast_id == "M2" else ("C3", "C6")
                )
                expected_order = baseline if replicate % 2 else tuple(reversed(baseline))
                self.assertEqual(
                    expected_order,
                    tuple(case.configuration_id for case in pair),
                )
                self.assertEqual(f"{contrast_id.lower()}-r{replicate:02d}", pair[0].pair_id)

    def test_run_case_uses_the_registered_pair_seed_when_overridden(self) -> None:
        pair_seed = 20361016
        history = History(
            manifest={"seed": pair_seed},
            operations=[],
            precondition={"status": "SATISFIED", "checks": []},
        )
        checked = SimpleNamespace(outcome=SimpleNamespace(value="PASS"))
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("run_campaign.MongoTrial") as trial_constructor,
                patch("run_campaign.run_property"),
                patch("run_campaign.write_history", return_value="history-hash"),
                patch("run_campaign.check_history", return_value=checked),
            ):
                trial = trial_constructor.return_value
                trial.history.return_value = history
                trial.__enter__.return_value = trial

                record = run_case(
                    campaign="rq3-m2",
                    ordinal=1,
                    configuration={"id": "C8"},
                    property_name="WFR",
                    adversarial=True,
                    seed_uris=(),
                    controller=None,
                    output_root=Path(directory),
                    runtime_metadata={"seed_base": 100},
                    seed_override=pair_seed,
                )

        self.assertEqual(pair_seed, trial_constructor.call_args.kwargs["seed"])
        self.assertEqual(pair_seed, record["seed"])

    def test_topology_plan_serializes_member_roles_and_routes(self) -> None:
        self.assertEqual(
            {
                "initial_primary": "mongo3",
                "isolation_target": "mongo3",
                "expected_new_primary": "mongo2",
                "subject_read_member": "mongo3",
                "first_write_member": "mongo3",
                "second_write_member": "mongo2",
                "election_guard_member": "mongo1",
            },
            TOPOLOGY_PLANS["M2"].to_dict(),
        )

    def test_partial_pair_is_not_treated_as_a_completed_invalid_pair(self) -> None:
        pair = pair_control(
            "M1",
            "m1-r01",
            {"C5": {"manifest": {}}},
            configurations=self.configurations,
        )

        self.assertFalse(_pair_is_complete(pair))

    def test_normalizer_forces_named_primary_then_releases_temporary_freeze(self) -> None:
        oracle = FakeOracle("mongo1")
        controller = FakeController()

        state = normalize_topology(
            oracle,
            controller,
            TOPOLOGY_PLANS["M1"],
            event_id="test-normalize",
            timeout_seconds=1,
        )

        self.assertEqual("mongo3", state["primary"])
        self.assertEqual(("mongo1", 15), oracle.step_down_calls[0])
        self.assertEqual("mongo1", state["step_down_member"])
        self.assertEqual(15, state["step_down_command_seconds"])
        self.assertIn(("mongo2", 120), oracle.freeze_calls)
        self.assertIn(("mongo2", 0), oracle.freeze_calls)
        self.assertEqual(
            [(["mongo1", "mongo2", "mongo3"], "test-normalize")],
            controller.heal_calls,
        )

    def test_normalizer_fails_when_heal_barrier_is_not_verified(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "could not heal"):
            normalize_topology(
                FakeOracle("mongo3"),
                FakeController(healthy=False),
                TOPOLOGY_PLANS["M1"],
                event_id="test-normalize",
                timeout_seconds=1,
            )

    def test_resume_rejects_protocol_and_topology_hash_changes(self) -> None:
        metadata = {
            "seed_base": 10,
            "runner_commit": "commit",
            "protocol_hash": "protocol-hash",
        }
        cases = _plan(8, 100_010)
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "results" / "raw" / "rq3"
            output_root.mkdir(parents=True)
            manifest_path = output_root / "campaign-manifest.json"
            with patch("run_rq3_campaign.campaign_runtime_metadata", return_value=metadata), patch(
                "run_rq3_campaign._require_frozen_provenance"
            ), patch("run_rq3_campaign._preflight_digest", return_value="a" * 64):
                payload = _manifest_payload(
                    status="RUNNING",
                    repetitions=8,
                    seed_base=100_010,
                    metadata=metadata,
                    cases=cases,
                    records={},
                    configurations=self.configurations,
                    started_ns=1,
                    preflight_sha256="a" * 64,
                    anchor_manifest_sha256=_sha256(ROOT / "configs/rq3-anchors.json"),
                )
                payload["schema_version"] = "rq3-campaign.v1"
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "frozen inputs"):
                    run_campaign(
                        output_root=output_root,
                        repetitions=8,
                        seed_base=100_010,
                        resume=True,
                    )

                payload["schema_version"] = "rq3-campaign.v2"
                payload["protocol_id"] = "rq3-protocol.v2"
                payload["topology_plan_sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "frozen inputs"):
                    run_campaign(
                        output_root=output_root,
                        repetitions=8,
                        seed_base=100_010,
                        resume=True,
                    )


if __name__ == "__main__":
    unittest.main()
