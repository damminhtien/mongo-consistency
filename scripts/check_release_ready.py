"""Reject tagged releases with incomplete evidence or student metadata."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

if __package__:
    from .build_submission import BuildError, parse_metadata
else:
    from build_submission import BuildError, parse_metadata

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CAMPAIGNS = {"pilot": 192, "experiment": 1280, "rq2": 432}
RQ2_CONFIGURATIONS = {"C1", "C3", "C4", "C6"}
EXPECTED_RQ2_CELLS = {property_name: RQ2_CONFIGURATIONS for property_name in ("RYW", "MR", "MW", "WFR")}
RQ2_CONDITIONS = ("F1", "F2", "F3")
RQ2_SIGNATURE_CELLS = {("C1", "RYW"), ("C6", "RYW"), ("C1", "MW"), ("C6", "MW")}
RQ2_EPISODE_IDS = {
    f"rq2-{condition.lower()}-r{repetition:02d}"
    for repetition in range(1, 9)
    for condition in RQ2_CONDITIONS
} | {f"rq2-f3-r{repetition:02d}" for repetition in range(9, 21)}
RQ2_EPISODE_COUNTS = {"F1": 8, "F2": 8, "F3": 20}
RQ2_EPISODE_PLAN = {
    f"rq2-{condition.lower()}-r{repetition:02d}": {
        "topology_condition": condition,
        "repetition": repetition,
        "signature_extension": False,
        "history_count": 16,
    }
    for repetition in range(1, 9)
    for condition in RQ2_CONDITIONS
} | {
    f"rq2-f3-r{repetition:02d}": {
        "topology_condition": "F3",
        "repetition": repetition,
        "signature_extension": True,
        "history_count": 4,
    }
    for repetition in range(9, 21)
}
EXPECTED_SUMMARY_COUNTS = {"pilot": 192, "experiment": 1280, "rq2": 432}


def _read_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read JSON: {error}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a JSON object")
        return None
    return value


def _check_campaign(root: Path, campaign: str, expected_count: int, errors: list[str]) -> None:
    path = root / "results/raw" / campaign / "campaign-manifest.json"
    manifest = _read_object(path, errors)
    if manifest is None:
        return
    if manifest.get("status") != "COMPLETE":
        errors.append(f"{campaign}: status must be COMPLETE")
    for field in ("expected_case_count", "completed_case_count", "case_count"):
        if manifest.get(field) != expected_count:
            errors.append(f"{campaign}: {field} must be {expected_count}")
    if manifest.get("runner_dirty") is not False:
        errors.append(f"{campaign}: runner provenance is missing or dirty")
    if not manifest.get("runner_commit"):
        errors.append(f"{campaign}: runner commit is missing")

    records = manifest.get("records")
    if not isinstance(records, list):
        errors.append(f"{campaign}: records must be a list")
        return
    if len(records) != expected_count:
        errors.append(f"{campaign}: expected {expected_count} records, found {len(records)}")
        return
    record_objects = [record for record in records if isinstance(record, dict)]
    if len(record_objects) != len(records):
        errors.append(f"{campaign}: records contain a non-object entry")
    ordinals = [record.get("ordinal") for record in record_objects]
    valid_ordinals = [value for value in ordinals if type(value) is int]
    if len(valid_ordinals) != expected_count or set(valid_ordinals) != set(range(1, expected_count + 1)):
        errors.append(f"{campaign}: record ordinals are incomplete or duplicated")
    if any(
        not isinstance(record, dict)
        or record.get("outcome") == "HARNESS_ERROR"
        or record.get("runner_error") is not None
        for record in records
    ):
        errors.append(f"{campaign}: contains a harness or runner error")

    if campaign in {"pilot", "experiment"}:
        counts = Counter(
            (
                record.get("configuration_id"),
                record.get("property"),
                bool(record.get("adversarial")),
            )
            for record in records
            if isinstance(record, dict)
        )
        for configuration_number in range(1, 9):
            configuration_id = f"C{configuration_number}"
            for property_name in EXPECTED_RQ2_CELLS:
                repetitions_by_mode = (
                    ((False, 1), (True, 5))
                    if campaign == "pilot"
                    else ((False, 10), (True, 30))
                )
                for adversarial, repetitions in repetitions_by_mode:
                    if counts[(configuration_id, property_name, adversarial)] != repetitions:
                        errors.append(
                            f"{campaign}: {configuration_id}/{property_name}/"
                            f"adversarial={adversarial} must have {repetitions} records"
                        )
    else:
        records_by_episode: dict[str, list[dict[str, Any]]] = {}
        counts = Counter(
            (
                record.get("topology_condition"),
                record.get("configuration_id"),
                record.get("property"),
            )
            for record in records
            if isinstance(record, dict)
        )
        expected_cells = {
            (condition, configuration_id, property_name)
            for condition in RQ2_CONDITIONS
            for property_name, configurations in EXPECTED_RQ2_CELLS.items()
            for configuration_id in configurations
        }
        if set(counts) != expected_cells:
            errors.append("rq2: topology-condition/configuration/property cells are incomplete or unexpected")
        for condition, configuration_id, property_name in sorted(expected_cells):
            expected_repetitions = (
                20
                if condition == "F3" and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
                else 8
            )
            if counts[(condition, configuration_id, property_name)] != expected_repetitions:
                errors.append(
                    f"rq2: {condition}/{configuration_id}/{property_name} must have "
                    f"{expected_repetitions} records"
                )
        repetitions = Counter(
            (
                record.get("topology_condition"),
                record.get("configuration_id"),
                record.get("property"),
                record.get("repetition"),
            )
            for record in records
            if isinstance(record, dict)
        )
        expected_repetitions = {
            (condition, configuration_id, property_name, repetition)
            for condition in RQ2_CONDITIONS
            for configuration_id in RQ2_CONFIGURATIONS
            for property_name in EXPECTED_RQ2_CELLS
            for repetition in range(
                1,
                21 if condition == "F3" and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS else 9,
            )
        }
        if set(repetitions) != expected_repetitions or any(count != 1 for count in repetitions.values()):
            errors.append("rq2: repetition indices are incomplete, duplicated, or outside the registered plan")
        for record in record_objects:
            condition = record.get("topology_condition")
            repetition = record.get("repetition")
            expected_episode_id = (
                f"rq2-{condition.lower()}-r{repetition:02d}"
                if isinstance(condition, str)
                and isinstance(repetition, int)
                and not isinstance(repetition, bool)
                else None
            )
            if record.get("fault_episode_id") != expected_episode_id:
                errors.append(f"rq2: history {record.get('trial_id')} has a mismatched fault_episode_id")
            expected_plan = RQ2_EPISODE_PLAN.get(expected_episode_id)
            configuration_id = record.get("configuration_id")
            property_name = record.get("property")
            if expected_plan is not None:
                is_signature_extension = (
                    expected_plan["signature_extension"] is True
                    and isinstance(configuration_id, str)
                    and isinstance(property_name, str)
                    and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
                )
                if record.get("signature_extension") is not is_signature_extension:
                    errors.append(f"rq2: history {record.get('trial_id')} has a mismatched signature flag")
                trial_id = record.get("trial_id")
                if isinstance(trial_id, str):
                    records_by_episode.setdefault(expected_episode_id, []).append(record)

        episodes = manifest.get("episodes")
        episode_entries = episodes if isinstance(episodes, list) else []
        episode_ids = [
            episode.get("episode_id")
            for episode in episode_entries
            if isinstance(episode, dict) and isinstance(episode.get("episode_id"), str)
        ]
        planned_episode_ids = manifest.get("planned_episode_ids")
        episode_plan_valid = (
            manifest.get("fault_episode_count") == len(RQ2_EPISODE_IDS)
            and manifest.get("completed_fault_episode_count") == len(RQ2_EPISODE_IDS)
            and isinstance(planned_episode_ids, list)
            and len(planned_episode_ids) == len(RQ2_EPISODE_IDS)
            and all(isinstance(episode_id, str) for episode_id in planned_episode_ids)
            and set(planned_episode_ids) == RQ2_EPISODE_IDS
            and len(episode_entries) == len(RQ2_EPISODE_IDS)
            and set(episode_ids) == RQ2_EPISODE_IDS
            and len(episode_ids) == len(set(episode_ids))
        )
        if not episode_plan_valid:
            errors.append("rq2: campaign manifest must contain all 36 registered fault episodes")
        for episode in episode_entries:
            if not isinstance(episode, dict):
                continue
            episode_id = episode.get("episode_id")
            expected_plan = RQ2_EPISODE_PLAN.get(episode_id) if isinstance(episode_id, str) else None
            if expected_plan is None:
                continue
            event = episode.get("event")
            if (
                episode.get("fault_status") != "APPLIED"
                or episode.get("recovery_status") != "CONVERGED"
                or episode.get("error") is not None
                or not isinstance(event, dict)
                or event.get("status") != "APPLIED"
                or event.get("recovery_status") != "CONVERGED"
            ):
                errors.append(f"rq2: {episode_id} lacks verified fault application or recovery")
            if expected_plan["topology_condition"] == "F1" and (
                not isinstance(event, dict) or event.get("fault_verified") is not True
            ):
                errors.append(f"rq2: {episode_id} lacks surviving-topology verification")
            if expected_plan["topology_condition"] == "F3":
                apply = event.get("coordinator_apply") if isinstance(event, dict) else None
                details = apply.get("details") if isinstance(apply, dict) else None
                controller = details.get("controller") if isinstance(details, dict) else None
                if (
                    not isinstance(controller, dict)
                    or controller.get("verified") is not True
                    or controller.get("replication_isolated") is not True
                ):
                    errors.append(f"rq2: {episode_id} lacks verified partition evidence")
            expected_history_count = expected_plan["history_count"]
            if (
                episode.get("history_count") != expected_history_count
                or episode.get("topology_condition") != expected_plan["topology_condition"]
                or episode.get("repetition") != expected_plan["repetition"]
                or episode.get("signature_extension") is not expected_plan["signature_extension"]
            ):
                errors.append(
                    f"rq2: {episode_id} metadata or history count differs from the registered plan"
                )
            episode_records = records_by_episode.get(episode_id, [])
            record_ids = [record.get("trial_id") for record in episode_records]
            listed_ids = episode.get("trial_ids")
            if (
                len(episode_records) != expected_history_count
                or not isinstance(listed_ids, list)
                or not all(isinstance(trial_id, str) for trial_id in listed_ids)
                or len(listed_ids) != len(set(listed_ids))
                or not all(isinstance(trial_id, str) for trial_id in record_ids)
                or set(listed_ids) != set(record_ids)
            ):
                errors.append(f"rq2: {episode_id} trial_ids do not match its history records")


def check_release_readiness(root: Path = ROOT) -> list[str]:
    """Return all reasons the current checkout must not be published as a release."""

    errors: list[str] = []
    for campaign, expected_count in EXPECTED_CAMPAIGNS.items():
        _check_campaign(root, campaign, expected_count, errors)

    summary = _read_object(root / "results/summary/summary.json", errors)
    if summary is not None:
        if summary.get("status") != "DATA":
            errors.append("analysis summary status must be DATA")
        campaigns = summary.get("campaign_summaries", {})
        if not isinstance(campaigns, dict):
            errors.append("analysis summary campaign_summaries must be an object")
        else:
            for campaign, expected_count in EXPECTED_SUMMARY_COUNTS.items():
                record = campaigns.get(campaign)
                actual = record.get("history_count") if isinstance(record, dict) else None
                if actual != expected_count:
                    errors.append(
                        f"analysis summary: {campaign} must contain {expected_count} histories"
                    )
        rq2_episodes = summary.get("fault_episode_summaries")
        if not isinstance(rq2_episodes, list):
            errors.append("analysis summary: fault_episode_summaries must be a list")
        else:
            episode_counts = {
                episode.get("topology_condition"): episode.get("episode_count")
                for episode in rq2_episodes
                if isinstance(episode, dict)
            }
            if len(rq2_episodes) != len(RQ2_EPISODE_COUNTS) or episode_counts != RQ2_EPISODE_COUNTS:
                errors.append("analysis summary: RQ2 must summarize 8/8/20 episodes for F1/F2/F3")

        groups = summary.get("groups")
        rq2_groups = [
            group
            for group in groups
            if isinstance(group, dict) and group.get("campaign_id") == "rq2"
        ] if isinstance(groups, list) else []
        if len(rq2_groups) != 48:
            errors.append("analysis summary: RQ2 must contain 48 condition/configuration/property groups")
        group_cells = {
            (
                group.get("topology_condition"),
                group.get("configuration_id"),
                group.get("property"),
            )
            for group in rq2_groups
        }
        expected_group_cells = {
            (condition, configuration_id, property_name)
            for condition in RQ2_CONDITIONS
            for property_name, configurations in EXPECTED_RQ2_CELLS.items()
            for configuration_id in configurations
        }
        if group_cells != expected_group_cells:
            errors.append("analysis summary: RQ2 condition/configuration/property groups are incomplete")
        for group in rq2_groups:
            baseline = group.get("normal_baseline")
            if (
                not isinstance(baseline, dict)
                or baseline.get("campaign_id") != "experiment"
                or baseline.get("history_count") != 10
            ):
                errors.append(
                    "analysis summary: every RQ2 group must carry its separate 10-history RQ1 normal baseline"
                )

    try:
        metadata = parse_metadata(root / "submission/metadata.mk")
    except BuildError as error:
        errors.append(str(error))
    else:
        members = [member.strip() for member in metadata["TEAM_MEMBERS"].split(";")]
        if len(members) != 3:
            errors.append("submission metadata must list exactly three team members")
        student_id = re.compile(r"\b[A-Z]\d{7}[A-Z]\b")
        for member in members:
            if not student_id.search(member) or re.search(r"pending|tbd|unknown", member, re.I):
                errors.append(f"submission metadata has a missing student ID: {member}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = check_release_readiness(args.root.resolve())
    if errors:
        print("Release readiness check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Release inputs are complete and ready to package.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
