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
EXPECTED_CAMPAIGNS = {"pilot": 192, "experiment": 1280, "rq2": 440}
EXPECTED_RQ2_CELLS = {
    "RYW": {"C1", "C6"},
    "MR": {"C1", "C3", "C6"},
    "MW": {"C1", "C4", "C6"},
    "WFR": {"C1", "C3", "C6"},
}
RQ2_CONDITIONS = (
    "NORMAL",
    "SECONDARY_FAILURE",
    "PRIMARY_FAILURE",
    "NETWORK_PARTITION",
)
EXPECTED_SUMMARY_COUNTS = {"pilot": 192, "experiment": 1280, "rq2": 440}


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
            if counts[(condition, configuration_id, property_name)] != 10:
                errors.append(
                    f"rq2: {condition}/{configuration_id}/{property_name} must have 10 records"
                )


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
