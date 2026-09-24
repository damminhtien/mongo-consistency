"""Compile the authored report and package it with the MongoDB runtime code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = Path("submission/metadata.mk")
PACKAGE_README_PATH = Path("submission/README.md")
PACKAGE_MAKEFILE_PATH = Path("submission/Makefile")
REQUIRED_SUBMISSION_FILES = (
    Path("submission/report.tex"),
    Path("submission/report.bib"),
    Path("submission/metadata.mk"),
    PACKAGE_README_PATH,
    PACKAGE_MAKEFILE_PATH,
)
REQUIRED_SECTIONS = tuple(
    Path("submission/sections") / f"{number:02d}-{name}.tex"
    for number, name in (
        (0, "acknowledgements"),
        (0, "abbreviations"),
        (1, "abstract"),
        (2, "introduction"),
        (3, "background"),
        (4, "deployment"),
        (5, "predictions"),
        (6, "method"),
        (7, "results"),
        (8, "discussion"),
        (10, "reproduction"),
        (11, "conclusion"),
        (12, "tool-use"),
        (13, "appendix"),
    )
)
METADATA_KEYS = (
    "COURSE_CODE",
    "COURSE_TITLE",
    "PROJECT_SUPERVISOR",
    "PROJECT_TITLE",
    "ACADEMIC_YEAR",
    "TEAM_NAME",
    "TEAM_MEMBERS",
    "STUDENT_EMAIL_MEMBER",
    "STUDENT_EMAIL",
    "SUBMISSION_DATE",
    "AI_USE_DISCLOSURE",
)
REPORT_FILENAME = "mongo-consistency-report.pdf"
ARCHIVE_FILENAME = "mongo-consistency-submission.zip"
FIGURE_INPUT_RE = re.compile(r"\\maybefigure(?:\[[^]]*\])?\{([^}]+)\}")
PACKAGE_ROOT_FILES = (
    "compose.yaml",
    "pyproject.toml",
    "requirements.txt",
    "docs/experimental-protocol.md",
)
PACKAGE_ROOT_DIRS = (
    "configs",
    "infra",
    "schemas",
    "src",
)
PACKAGE_RUNTIME_SCRIPTS = (
    "scripts/package_provenance.py",
    "scripts/setup_experiment.py",
    "scripts/initialize_replica_set.py",
    "scripts/run_campaign.py",
    "scripts/run_rq2_coordinator.py",
    "scripts/run_rq2_campaign.py",
    "scripts/run_rq3_campaign.py",
    "scripts/run_rq3_preflight.py",
)
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "graphify-out",
        "node_modules",
        "output",
        "tmp",
        "venv",
        ".venv",
    }
)
EXCLUDED_RELATIVE_PATHS = (
    Path("results/archive"),
    Path("results/smoke"),
    Path("results/smoke-dport"),
    Path("results/smoke-verified"),
    Path("src/mongo_consistency/analysis.py"),
    Path("src/mongo_consistency/figures.py"),
    Path("src/mongo_consistency/rq4.py"),
)


class BuildError(RuntimeError):
    """A user-actionable submission build failure."""


def _remove_generated(path: Path) -> None:
    """Remove one exact generated path and refuse symlink targets."""

    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise BuildError(f"Refusing to remove symlink at generated path: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def parse_metadata(path: Path) -> dict[str, str]:
    """Parse the small, non-shell metadata file used by the report."""

    values: dict[str, str] = {}
    assignment = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(.*)")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise BuildError(f"Cannot read metadata file {path}: {error}") from error

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment.fullmatch(stripped)
        if not match:
            raise BuildError(
                f"Invalid metadata assignment at {path}:{line_number}: {line!r}"
            )
        key, value = match.groups()
        values[key] = value.strip()

    missing = [key for key in METADATA_KEYS if not values.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise BuildError(f"Metadata fields are empty: {joined}")
    return values


def escape_latex(value: str) -> str:
    """Escape a numeric or table value for a generated data macro."""

    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return re.sub(r"[\\&%$#_{}~^]", lambda match: replacements[match.group()], value)


def _format_fault_outcomes(outcomes: dict[str, Any]) -> str:
    """Format fault outcomes in the order used by the report table."""

    names = ("PASS", "VIOLATION", "UNAVAILABLE", "INDETERMINATE", "PRECONDITION_MISS")
    return "/".join(str(int(outcomes.get(name, 0) or 0)) for name in names)


def _compact_fault_outcomes(outcomes: dict[str, Any]) -> str:
    """Show only nonzero counts in a configuration-property cell."""

    codes = (
        ("PASS", "P"),
        ("VIOLATION", "V"),
        ("UNAVAILABLE", "U"),
        ("INDETERMINATE", "I"),
        ("PRECONDITION_MISS", "PM"),
        ("HARNESS_ERROR", "HE"),
    )
    parts = [
        f"{int(outcomes.get(outcome, 0) or 0)}{code}"
        for outcome, code in codes
        if int(outcomes.get(outcome, 0) or 0) > 0
    ]
    return ", ".join(parts) or "NO_DATA"


def write_generated_analysis(path: Path, root: Path) -> None:
    """Write main-campaign report macros, keeping pilot and test modes separate."""

    summary_path = root / "results/summary/summary.json"
    status = "NO_DATA"
    pilot_history_count = 0
    main_campaign: dict[str, Any] = {}
    rq2_campaign: dict[str, Any] = {}
    summary_groups: list[dict[str, Any]] = []
    fault_episode_summaries: list[dict[str, Any]] = []
    normal: dict[str, Any] = {}
    adversarial: dict[str, Any] = {}
    prediction_manifest: dict[str, dict[str, Any]] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            summary = {}
        if isinstance(summary, dict):
            status = str(summary.get("status", status))
            campaigns = summary.get("campaign_summaries", {})
            if isinstance(campaigns, dict):
                candidate = campaigns.get("experiment", {})
                if isinstance(candidate, dict):
                    main_campaign = candidate
                    normal_candidate = candidate.get("normal", {})
                    adversarial_candidate = candidate.get("adversarial", {})
                    if isinstance(normal_candidate, dict):
                        normal = normal_candidate
                    if isinstance(adversarial_candidate, dict):
                        adversarial = adversarial_candidate
                pilot = campaigns.get("pilot", {})
                if isinstance(pilot, dict):
                    pilot_history_count = int(pilot.get("history_count", 0) or 0)
                rq2 = campaigns.get("rq2", {})
                if isinstance(rq2, dict):
                    rq2_campaign = rq2
            groups = summary.get("groups", [])
            if isinstance(groups, list):
                summary_groups = [row for row in groups if isinstance(row, dict)]
            predictions = summary.get("predictions", {})
            if isinstance(predictions, dict):
                prediction_manifest = {
                    str(configuration_id): prediction
                    for configuration_id, prediction in predictions.items()
                    if isinstance(prediction, dict)
                }
            episodes = summary.get("fault_episode_summaries", [])
            if isinstance(episodes, list):
                fault_episode_summaries = [
                    row for row in episodes if isinstance(row, dict)
                ]

    def metric(summary: dict[str, Any], keys: tuple[str, ...]) -> str:
        value: object = summary
        for key in keys:
            if not isinstance(value, dict):
                return "NO_DATA"
            value = value.get(key)
        if value is None:
            return "NA" if summary.get("history_count", 0) else "NO_DATA"
        if isinstance(value, (int, float)):
            return f"{value:.4f}"
        return str(value)

    main_manifest: dict[str, Any] = {}
    manifest_path = root / "results/raw/experiment/campaign-manifest.json"
    if manifest_path.is_file():
        try:
            loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded_manifest, dict):
                main_manifest = loaded_manifest
        except (OSError, UnicodeError, json.JSONDecodeError):
            main_manifest = {}
    main_campaign_status = str(main_manifest.get("status", "NO_DATA"))

    rq2_manifest: dict[str, Any] = {}
    rq2_manifest_path = root / "results/raw/rq2/campaign-manifest.json"
    if rq2_manifest_path.is_file():
        try:
            loaded_manifest = json.loads(rq2_manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded_manifest, dict):
                rq2_manifest = loaded_manifest
        except (OSError, UnicodeError, json.JSONDecodeError):
            rq2_manifest = {}
    rq2_manifest_status = str(rq2_manifest.get("status", "NO_DATA"))
    software_versions = main_manifest.get("software_versions", {})
    if not isinstance(software_versions, dict):
        software_versions = {}
    setup_actual: dict[str, Any] = {}
    setup_path = root / "results/setup/toolchain.json"
    if setup_path.is_file():
        try:
            setup_data = json.loads(setup_path.read_text(encoding="utf-8"))
            if isinstance(setup_data, dict) and isinstance(setup_data.get("actual"), dict):
                setup_actual = setup_data["actual"]
        except (OSError, UnicodeError, json.JSONDecodeError):
            setup_actual = {}

    def version_value(key: str) -> str:
        value = software_versions.get(key)
        return str(value) if value else "not recorded"

    image_digests = main_manifest.get("image_digest", [])
    if isinstance(image_digests, list):
        image_digest = ", ".join(str(value) for value in image_digests if value)
    else:
        image_digest = str(image_digests) if image_digests else "not recorded"
    rq2_groups = [row for row in summary_groups if row.get("campaign_id") == "rq2"]
    rq1_adversarial_groups = {
        (str(row.get("configuration_id")), str(row.get("property"))): row
        for row in summary_groups
        if row.get("campaign_id") == "experiment" and row.get("adversarial") is True
    }
    rq1_observation_codes = (
        ("P", "PASS"),
        ("V", "VIOLATION"),
        ("U", "UNAVAILABLE"),
        ("I", "INDETERMINATE"),
        ("PM", "PRECONDITION_MISS"),
        ("HE", "HARNESS_ERROR"),
    )

    def rq1_prediction_observation_rows() -> str:
        rows: list[str] = []
        for configuration_id in (f"C{number}" for number in range(1, 9)):
            prediction = prediction_manifest.get(configuration_id, {})
            targets = prediction.get("guarantee_targets", [])
            targets = set(targets) if isinstance(targets, list) else set()
            cells: list[str] = []
            for property_name in ("RYW", "MR", "MW", "WFR"):
                group = rq1_adversarial_groups.get((configuration_id, property_name))
                if not group:
                    cells.append("--")
                    continue
                counts = group.get("outcomes", {})
                counts = counts if isinstance(counts, dict) else {}
                observed = [
                    f"{code}={int(counts.get(outcome, 0) or 0)}"
                    for code, outcome in rq1_observation_codes
                    if int(counts.get(outcome, 0) or 0) > 0
                ]
                prediction_code = "G" if property_name in targets else "N"
                cells.append(f"{prediction_code}; " + (" ".join(observed) or "no classified outcome"))
            rows.append(configuration_id + " & " + " & ".join(cells) + r" \\")
        return "\n".join(rows)

    rq2_group_by_key = {
        (
            str(row.get("topology_condition")),
            str(row.get("configuration_id")),
            str(row.get("property")),
        ): row
        for row in rq2_groups
    }
    rq2_faults = ("F1", "F2", "F3")
    rq2_configurations = ("C1", "C3", "C4", "C6")
    rq2_properties = ("RYW", "MR", "MW", "WFR")
    rq2_fault_macro_names = {"F1": "Fone", "F2": "Ftwo", "F3": "Fthree"}
    rq2_config_macro_names = {
        "C1": "Cone",
        "C3": "Cthree",
        "C4": "Cfour",
        "C6": "Csix",
    }
    rq2_expected_keys = {
        (fault, configuration, property_name)
        for fault in rq2_faults
        for configuration in rq2_configurations
        for property_name in rq2_properties
    }
    rq2_episode_by_fault = {
        str(row.get("topology_condition")): row
        for row in fault_episode_summaries
        if row.get("topology_condition") in rq2_faults
    }
    rq2_complete = (
        rq2_manifest_status == "COMPLETE"
        and len(rq2_groups) == len(rq2_expected_keys)
        and set(rq2_group_by_key) == rq2_expected_keys
        and set(rq2_episode_by_fault) == set(rq2_faults)
    )

    def count_value(summary: dict[str, Any], field: str) -> int:
        value = summary.get(field, 0)
        return int(value) if isinstance(value, (int, float)) else 0

    def percent_value(value: object, digits: int = 1) -> str:
        if not isinstance(value, (int, float)):
            return "NA" if rq2_complete else "--"
        return f"{float(value) * 100:.{digits}f}"

    def millisecond_value(value: object, digits: int = 1) -> str:
        return f"{float(value):.{digits}f}" if isinstance(value, (int, float)) else "NA"

    def second_value(value: object) -> str:
        return f"{float(value) / 1000:.2f}" if isinstance(value, (int, float)) else "NA"

    normal_counts = normal.get("outcomes", {})
    adversarial_counts = adversarial.get("outcomes", {})
    property_summaries = main_campaign.get("properties", {})
    outcome_names = {
        "PASS": "Pass",
        "VIOLATION": "Violation",
        "UNAVAILABLE": "Unavailable",
        "INDETERMINATE": "Indeterminate",
        "PRECONDITION_MISS": "PreconditionMiss",
        "HARNESS_ERROR": "HarnessError",
    }
    adversarial_pass = int(adversarial_counts.get("PASS", 0) or 0)
    adversarial_violation = int(adversarial_counts.get("VIOLATION", 0) or 0)
    has_normal_histories = int(normal.get("history_count", 0) or 0) > 0
    has_adversarial_histories = int(adversarial.get("history_count", 0) or 0) > 0
    experiment_history_count = int(main_campaign.get("history_count", 0) or 0)
    macros = {
        "AnalysisStatus": status,
        "HistoryCount": str(experiment_history_count),
        "PilotHistoryCount": str(pilot_history_count),
        "NormalHistoryCount": str(int(normal.get("history_count", 0) or 0)),
        "AdversarialHistoryCount": str(int(adversarial.get("history_count", 0) or 0)),
        "AdversarialDecidableCount": (
            str(adversarial_pass + adversarial_violation)
            if has_adversarial_histories
            else "--"
        ),
        "MainCampaignStatus": main_campaign_status,
        **{
            f"{cohort}{outcome_name}Count": (
                str(int(counts.get(outcome, 0) or 0))
                if has_histories
                else "--"
            )
            for cohort, counts, has_histories in (
                ("Normal", normal_counts, has_normal_histories),
                ("Adversarial", adversarial_counts, has_adversarial_histories),
            )
            for outcome, outcome_name in outcome_names.items()
        },
        "ConsistencyViolationRate": metric(adversarial, ("consistency_violation_rate",)),
        "ControlConsistencyViolationRate": metric(normal, ("consistency_violation_rate",)),
        "OperationSuccessRate": metric(adversarial, ("operation_success_rate",)),
        "ControlOperationSuccessRate": metric(normal, ("operation_success_rate",)),
        "HistoryCompletionRate": metric(adversarial, ("history_completion_rate",)),
        "ControlHistoryCompletionRate": metric(normal, ("history_completion_rate",)),
        "LatencyMedian": metric(adversarial, ("latency_ms", "p50")),
        "LatencyHigh": metric(adversarial, ("latency_ms", "p95")),
        "LatencyTail": metric(adversarial, ("latency_ms", "p99")),
        "ControlLatencyMedian": metric(normal, ("latency_ms", "p50")),
        "ControlLatencyHigh": metric(normal, ("latency_ms", "p95")),
        "ControlLatencyTail": metric(normal, ("latency_ms", "p99")),
        "ElectionMedian": metric(adversarial, ("election_ms", "p50")),
        "ControlElectionMedian": metric(normal, ("election_ms", "p50")),
        "RecoveryMedian": metric(adversarial, ("recovery_ms", "p50")),
        "ControlRecoveryMedian": metric(normal, ("recovery_ms", "p50")),
        "MongoDBImage": (
            f"mongo:{version_value('mongodb')}"
            if version_value("mongodb") != "not recorded"
            else "not recorded"
        ),
        "MongoDBServerVersion": version_value("mongodb"),
        "PyMongoVersion": version_value("pymongo"),
        "RunnerPythonVersion": version_value("python"),
        "DockerEngineVersion": version_value("docker_engine"),
        "DockerComposeVersion": version_value("docker_compose"),
        "DockerKernelVersion": str(setup_actual.get("docker_kernel") or "not recorded"),
        "MongoImageDigest": image_digest or "not recorded",
        "HostOSVersion": "not recorded in campaign manifests",
    }
    if isinstance(property_summaries, dict):
        for property_name in ("RYW", "MR", "MW", "WFR"):
            property_summary = property_summaries.get(property_name, {})
            if not isinstance(property_summary, dict):
                property_summary = {}
            property_counts = property_summary.get("outcomes", {})
            if not isinstance(property_counts, dict):
                property_counts = {}
            pass_count = int(property_counts.get("PASS", 0) or 0)
            violation_count = int(property_counts.get("VIOLATION", 0) or 0)
            macros[f"{property_name}HistoryCount"] = str(
                int(property_summary.get("history_count", 0) or 0)
            )
            has_property_histories = int(property_summary.get("history_count", 0) or 0) > 0
            macros[f"{property_name}ViolationCount"] = (
                str(violation_count) if has_property_histories else "--"
            )
            macros[f"{property_name}DecidableCount"] = (
                str(pass_count + violation_count) if has_property_histories else "--"
            )
            for outcome, macro_suffix in outcome_names.items():
                macros[f"{property_name}{macro_suffix}Count"] = str(
                    int(property_counts.get(outcome, 0) or 0)
                ) if has_property_histories else "--"
    rq2_raw_macros = {
        "RQOnePredictionObservationRows": rq1_prediction_observation_rows(),
        "RQTwoFthreeOutcomeRows": r"\multicolumn{5}{c}{\texttt{NO DATA}} \\",
        "RQTwoFthreeMetricRows": r"\multicolumn{5}{c}{\texttt{NO DATA}} \\",
        "RQTwoEpisodeRows": "",
        "RQTwoFaultSummaryRows": "",
    }
    macros["RQTwoStatus"] = rq2_manifest_status
    macros["RQTwoFthreeSuccessfulLatencyHighMs"] = "--"
    macros["RQTwoFthreeIndeterminateCount"] = "--"
    for property_name in rq2_properties:
        macros[f"RQTwo{property_name}HistoryCount"] = "--"
        for macro_suffix in outcome_names.values():
            macros[f"RQTwo{property_name}{macro_suffix}Count"] = "--"
    if rq2_complete:
        rq2_adversarial = rq2_campaign.get("adversarial", {})
        rq2_outcomes = rq2_campaign.get("outcomes", {})
        if not isinstance(rq2_adversarial, dict):
            rq2_adversarial = {}
        if not isinstance(rq2_outcomes, dict):
            rq2_outcomes = {}
        macros.update(
            {
                "RQTwoHistoryCount": str(count_value(rq2_campaign, "history_count")),
                "RQTwoDecidableHistoryCount": str(
                    int(rq2_outcomes.get("PASS", 0) or 0)
                    + int(rq2_outcomes.get("VIOLATION", 0) or 0)
                ),
                "RQTwoFaultEpisodeCount": str(
                    sum(
                        count_value(episode, "episode_count")
                        for episode in rq2_episode_by_fault.values()
                    )
                ),
                "RQTwoPassCount": str(int(rq2_outcomes.get("PASS", 0) or 0)),
                "RQTwoViolationCount": str(int(rq2_outcomes.get("VIOLATION", 0) or 0)),
                "RQTwoUnavailableCount": str(int(rq2_outcomes.get("UNAVAILABLE", 0) or 0)),
                "RQTwoIndeterminateCount": str(int(rq2_outcomes.get("INDETERMINATE", 0) or 0)),
                "RQTwoPreconditionMissCount": str(
                    int(rq2_outcomes.get("PRECONDITION_MISS", 0) or 0)
                ),
                "RQTwoHarnessErrorCount": str(int(rq2_outcomes.get("HARNESS_ERROR", 0) or 0)),
                "RQTwoConsistencyViolationPercent": percent_value(
                    rq2_adversarial.get("consistency_violation_rate")
                ),
                "RQTwoOperationSuccessPercent": percent_value(
                    rq2_adversarial.get("operation_success_rate")
                ),
                "RQTwoHistoryCompletionPercent": percent_value(
                    rq2_adversarial.get("history_completion_rate")
                ),
                "RQTwoOperationSuccessfulCount": str(
                    count_value(rq2_adversarial, "operation_successful_count")
                ),
                "RQTwoOperationAttemptedCount": str(
                    count_value(rq2_adversarial, "operation_attempted_count")
                ),
                "RQTwoLatencyMedianMs": millisecond_value(
                    (rq2_adversarial.get("latency_ms") or {}).get("p50")
                ),
                "RQTwoLatencyHighMs": millisecond_value(
                    (rq2_adversarial.get("latency_ms") or {}).get("p95")
                ),
                "RQTwoLatencyTailMs": millisecond_value(
                    (rq2_adversarial.get("latency_ms") or {}).get("p99")
                ),
                "RQTwoAcknowledgedWriteCount": str(
                    count_value(rq2_adversarial, "acknowledged_write_count")
                ),
                "RQTwoRollbackCheckedWriteCount": str(
                    count_value(rq2_adversarial, "rollback_checked_write_count")
                ),
                "RQTwoRolledBackWriteCount": str(
                    count_value(rq2_adversarial, "rolled_back_write_count")
                ),
                "RQTwoRollbackPercent": percent_value(
                    rq2_adversarial.get("acknowledged_write_rollback_rate")
                ),
            }
        )

        rq2_property_summaries = rq2_campaign.get("properties", {})
        if not isinstance(rq2_property_summaries, dict):
            rq2_property_summaries = {}
        for property_name in rq2_properties:
            property_summary = rq2_property_summaries.get(property_name, {})
            if not isinstance(property_summary, dict):
                property_summary = {}
            property_counts = property_summary.get("outcomes", {})
            if not isinstance(property_counts, dict):
                property_counts = {}
            macros[f"RQTwo{property_name}HistoryCount"] = str(
                count_value(property_summary, "history_count")
            )
            for outcome, macro_suffix in outcome_names.items():
                macros[f"RQTwo{property_name}{macro_suffix}Count"] = str(
                    int(property_counts.get(outcome, 0) or 0)
                )

        episode_table_rows: list[str] = []
        fault_summary_rows: list[str] = []
        for fault in rq2_faults:
            for configuration in rq2_configurations:
                for property_name in rq2_properties:
                    row = rq2_group_by_key[(fault, configuration, property_name)]
                    prefix = (
                        f"RQTwo{rq2_fault_macro_names[fault]}"
                        f"{rq2_config_macro_names[configuration]}{property_name}"
                    )
                    counts = row.get("outcomes", {})
                    if not isinstance(counts, dict):
                        counts = {}
                    macros[f"{prefix}HistoryCount"] = str(count_value(row, "history_count"))
                    for outcome, macro_suffix in outcome_names.items():
                        macros[f"{prefix}{macro_suffix}Count"] = str(
                            int(counts.get(outcome, 0) or 0)
                        )
                    macros[f"{prefix}OperationSuccessfulCount"] = str(
                        count_value(row, "operation_successful_count")
                    )
                    macros[f"{prefix}OperationAttemptedCount"] = str(
                        count_value(row, "operation_attempted_count")
                    )

        fthree_outcome_rows: list[str] = []
        for configuration in rq2_configurations:
            cells = []
            for property_name in rq2_properties:
                outcomes = rq2_group_by_key[("F3", configuration, property_name)].get(
                    "outcomes", {}
                )
                cells.append(
                    _compact_fault_outcomes(outcomes if isinstance(outcomes, dict) else {})
                )
            fthree_outcome_rows.append(
                f"{configuration} & {' & '.join(cells)} \\\\"
            )

        fthree_metric_rows: list[str] = []
        for configuration, property_name in (
            ("C1", "RYW"),
            ("C1", "MW"),
            ("C1", "WFR"),
            ("C4", "MW"),
            ("C4", "WFR"),
            ("C6", "RYW"),
            ("C6", "MW"),
        ):
            row = rq2_group_by_key[("F3", configuration, property_name)]
            latency = row.get("latency_ms", {})
            latency = latency if isinstance(latency, dict) else {}
            checked = count_value(row, "rollback_checked_write_count")
            rollback = (
                f"{count_value(row, 'rolled_back_write_count')}/{checked}"
                if checked
                else "NA"
            )
            fthree_metric_rows.append(
                f"{configuration} & {property_name} & "
                f"{millisecond_value(latency.get('p95'))} & "
                f"{count_value(row, 'acknowledged_write_count')} & {rollback} \\\\"
            )

        for fault in rq2_faults:
            fault_summary = (rq2_campaign.get("topology_conditions") or {}).get(fault, {})
            if not isinstance(fault_summary, dict):
                fault_summary = {}
            prefix = f"RQTwo{rq2_fault_macro_names[fault]}"
            fault_outcomes = fault_summary.get("outcomes", {})
            if not isinstance(fault_outcomes, dict):
                fault_outcomes = {}
            macros.update(
                {
                    f"{prefix}IndeterminateCount": str(
                        int(fault_outcomes.get("INDETERMINATE", 0) or 0)
                    ),
                    f"{prefix}OperationSuccessfulCount": str(
                        count_value(fault_summary, "operation_successful_count")
                    ),
                    f"{prefix}OperationAttemptedCount": str(
                        count_value(fault_summary, "operation_attempted_count")
                    ),
                    f"{prefix}OperationSuccessPercent": percent_value(
                        fault_summary.get("operation_success_rate")
                    ),
                    f"{prefix}HistoryCompletionPercent": percent_value(
                        fault_summary.get("history_completion_rate")
                    ),
                    f"{prefix}LatencyMedianMs": millisecond_value(
                        (fault_summary.get("latency_ms") or {}).get("p50")
                    ),
                    f"{prefix}LatencyHighMs": millisecond_value(
                        (fault_summary.get("latency_ms") or {}).get("p95")
                    ),
                    f"{prefix}LatencyTailMs": millisecond_value(
                        (fault_summary.get("latency_ms") or {}).get("p99")
                    ),
                    f"{prefix}SuccessfulLatencyHighMs": millisecond_value(
                        (fault_summary.get("successful_latency_ms") or {}).get("p95")
                    ),
                    f"{prefix}AcknowledgedWriteCount": str(
                        count_value(fault_summary, "acknowledged_write_count")
                    ),
                    f"{prefix}RollbackCheckedWriteCount": str(
                        count_value(fault_summary, "rollback_checked_write_count")
                    ),
                    f"{prefix}RolledBackWriteCount": str(
                        count_value(fault_summary, "rolled_back_write_count")
                    ),
                    f"{prefix}RollbackPercent": percent_value(
                        fault_summary.get("acknowledged_write_rollback_rate")
                    ),
                }
            )

            episode = rq2_episode_by_fault[fault]
            episode_records = episode.get("episodes", [])
            if not isinstance(episode_records, list):
                episode_records = []
            applied_count = sum(
                isinstance(item, dict) and item.get("status") == "APPLIED"
                for item in episode_records
            )
            converged_count = sum(
                isinstance(item, dict) and item.get("recovery_status") == "CONVERGED"
                for item in episode_records
            )
            macros.update(
                {
                    f"{prefix}EpisodeCount": str(count_value(episode, "episode_count")),
                    f"{prefix}AppliedEpisodeCount": str(applied_count),
                    f"{prefix}ElectionSuccessCount": str(
                        count_value(episode, "election_success_count")
                    ),
                    f"{prefix}ElectionMedianSeconds": second_value(
                        (episode.get("election_ms") or {}).get("p50")
                    ),
                    f"{prefix}RecoveryConvergedCount": str(converged_count),
                    f"{prefix}RecoveryMedianSeconds": second_value(
                        (episode.get("recovery_ms") or {}).get("p50")
                    ),
                }
            )
            episode_table_rows.append(
                f"{fault} & {count_value(episode, 'episode_count')} & {applied_count} & "
                f"{count_value(episode, 'election_success_count')} & "
                f"{second_value((episode.get('election_ms') or {}).get('p50'))} & "
                f"{converged_count} & "
                f"{second_value((episode.get('recovery_ms') or {}).get('p50'))} \\\\"
            )
            operation_successful = count_value(
                fault_summary, "operation_successful_count"
            )
            operation_attempted = count_value(
                fault_summary, "operation_attempted_count"
            )
            fault_summary_rows.append(
                f"{fault} & {count_value(fault_summary, 'history_count')} & "
                f"{count_value(episode, 'episode_count')} & "
                f"{_format_fault_outcomes(fault_outcomes)} & "
                f"{operation_successful}/{operation_attempted} "
                f"({percent_value(fault_summary.get('operation_success_rate'))}\\%) & "
                f"{millisecond_value((fault_summary.get('latency_ms') or {}).get('p50'))}/"
                f"{millisecond_value((fault_summary.get('latency_ms') or {}).get('p95'))} \\\\"
            )
        rq2_raw_macros.update(
            {
                "RQTwoFthreeOutcomeRows": "\n".join(fthree_outcome_rows),
                "RQTwoFthreeMetricRows": "\n".join(fthree_metric_rows),
                "RQTwoEpisodeRows": "\n".join(episode_table_rows),
                "RQTwoFaultSummaryRows": "\n".join(fault_summary_rows),
            }
        )
    else:
        macros.update(
            {
                "RQTwoHistoryCount": "--",
                "RQTwoDecidableHistoryCount": "--",
                "RQTwoFaultEpisodeCount": "--",
                "RQTwoPassCount": "--",
                "RQTwoViolationCount": "--",
                "RQTwoUnavailableCount": "--",
                "RQTwoIndeterminateCount": "--",
                "RQTwoPreconditionMissCount": "--",
                "RQTwoHarnessErrorCount": "--",
                "RQTwoConsistencyViolationPercent": "NO_DATA",
                "RQTwoOperationSuccessPercent": "NO_DATA",
                "RQTwoHistoryCompletionPercent": "NO_DATA",
                "RQTwoOperationSuccessfulCount": "--",
                "RQTwoOperationAttemptedCount": "--",
                "RQTwoLatencyMedianMs": "NO_DATA",
                "RQTwoLatencyHighMs": "NO_DATA",
                "RQTwoLatencyTailMs": "NO_DATA",
                "RQTwoAcknowledgedWriteCount": "--",
                "RQTwoRollbackCheckedWriteCount": "--",
                "RQTwoRolledBackWriteCount": "--",
                "RQTwoRollbackPercent": "NO_DATA",
            }
        )

    content = "\n".join(
        f"\\newcommand{{\\{name}}}{{{escape_latex(value)}}}"
        for name, value in macros.items()
    )
    content += "\n" + "\n".join(
        f"\\newcommand{{\\{name}}}{{{value}}}"
        for name, value in rq2_raw_macros.items()
    )
    path.write_text(content + "\n", encoding="utf-8")


def validate_layout(root: Path) -> None:
    """Check the source layout before starting a build."""

    required = (*REQUIRED_SUBMISSION_FILES, *REQUIRED_SECTIONS)
    missing = [path for path in required if not (root / path).is_file()]
    if missing:
        formatted = ", ".join(str(path) for path in missing)
        raise BuildError(f"Submission layout is incomplete; missing: {formatted}")


def validate_package_sources(root: Path) -> None:
    """Require every file and directory needed by the runtime package."""

    required_files = tuple(Path(path) for path in PACKAGE_ROOT_FILES) + tuple(
        Path(path) for path in PACKAGE_RUNTIME_SCRIPTS
    )
    missing_files = [path for path in required_files if not (root / path).is_file()]
    missing_dirs = [
        Path(path) for path in PACKAGE_ROOT_DIRS if not (root / path).is_dir()
    ]
    missing = [*missing_files, *missing_dirs]
    if missing:
        formatted = ", ".join(str(path) for path in missing)
        raise BuildError(f"Submission runtime source is incomplete; missing: {formatted}")


def _iter_files(root: Path, paths: Iterable[Path]) -> Iterable[tuple[Path, Path]]:
    """Yield source files and their repository-relative paths."""

    for relative in paths:
        source = root / relative
        if source.is_file() and not source.is_symlink():
            yield source, relative
            continue
        if not source.exists():
            continue
        if source.is_symlink():
            raise BuildError(f"Refusing to package symlink: {relative}")
        for candidate in sorted(source.rglob("*")):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            candidate_relative = candidate.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in candidate_relative.parts):
                continue
            if any(
                excluded == candidate_relative or excluded in candidate_relative.parents
                for excluded in EXCLUDED_RELATIVE_PATHS
            ):
                continue
            if candidate.suffix in {".pyc", ".pyo"}:
                continue
            yield candidate, candidate_relative


def copy_source_tree(root: Path, destination: Path) -> list[Path]:
    """Copy project source while excluding local build metadata."""

    destination.mkdir(parents=True, exist_ok=True)
    paths = [Path(path) for path in PACKAGE_ROOT_FILES]
    paths.extend(Path(path) for path in PACKAGE_ROOT_DIRS)
    paths.extend(Path(path) for path in PACKAGE_RUNTIME_SCRIPTS)
    copied: list[Path] = []
    for source, relative in _iter_files(root, paths):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative)
    return sorted(copied)


def _command_tail(output: str, limit: int = 1200) -> str:
    compact = output.strip()
    return compact[-limit:] if compact else "no diagnostic output"


def texlive_bin_dirs() -> tuple[Path, ...]:
    """Return configured and user-local TeX Live binary directories."""

    candidates: list[Path] = []
    configured = os.environ.get("TEXLIVE_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())
    user_texlive = Path.home() / "texlive"
    if user_texlive.is_dir():
        for version in sorted(user_texlive.iterdir()):
            bin_root = version / "bin"
            if bin_root.is_dir():
                candidates.extend(
                    path for path in sorted(bin_root.iterdir()) if path.is_dir()
                )
    return tuple(dict.fromkeys(path for path in candidates if path.is_dir()))


def find_tool(name: str) -> str | None:
    """Find a tool on PATH or in a user-local TeX Live installation."""

    found = shutil.which(name)
    if found:
        return found
    for directory in texlive_bin_dirs():
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_command(
    command: list[str],
    cwd: Path,
    log_path: Path,
    *,
    environment_overrides: dict[str, str] | None = None,
) -> None:
    """Run one build command and retain its complete output in a log."""

    environment = os.environ.copy()
    environment.setdefault("SOURCE_DATE_EPOCH", "0")
    if environment_overrides:
        environment.update(environment_overrides)
    command_directory = str(Path(command[0]).resolve().parent)
    path_entries = environment.get("PATH", "").split(os.pathsep)
    if command_directory not in path_entries:
        environment["PATH"] = os.pathsep.join(
            (command_directory, environment.get("PATH", ""))
        )
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"Command failed: {' '.join(command)}\n{error}") from error

    output = completed.stdout + completed.stderr
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(command)}\n{output}\n")
    if completed.returncode:
        raise BuildError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command)}\n"
            f"{_command_tail(output)}"
        )


def compile_report(root: Path, build_root: Path) -> Path:
    """Compile the report in an isolated temporary tree."""

    latex_root = build_root / "latex"
    source_target = latex_root / "submission"
    shutil.copytree(root / "submission", source_target)
    log_path = build_root / "latex-build.log"

    run_command(
        [sys.executable, "scripts/analyse_results.py"],
        root,
        log_path,
        environment_overrides={"PYTHONPATH": str(root / "src")},
    )
    run_command(
        [
            sys.executable,
            "scripts/analyse_rq4.py",
            "--data-output",
            str(source_target / "generated-rq4-data.tex"),
        ],
        root,
        log_path,
        environment_overrides={"PYTHONPATH": str(root / "src")},
    )
    generated_figures = root / "figures"
    target_figures = source_target / "figures"
    for figure in sorted(generated_figures.iterdir()):
        if figure.is_file() and figure.suffix.lower() == ".pdf":
            shutil.copy2(figure, target_figures / figure.name)
    validate_figure_inputs(latex_root)
    write_generated_analysis(source_target / "generated-analysis.tex", root)

    latexmk = find_tool("latexmk")
    engine = find_tool("pdflatex") or find_tool("xelatex") or find_tool("lualatex")
    bibtex = find_tool("bibtex")
    if latexmk and not bibtex:
        raise BuildError(
            "bibtex is required for this report. Install a TeX distribution and retry."
        )
    if latexmk:
        run_command(
            [
                latexmk,
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "submission/report.tex",
            ],
            latex_root,
            log_path,
        )
    elif engine:
        run_command(
            [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "submission/report.tex",
            ],
            latex_root,
            log_path,
        )
        if not bibtex:
            raise BuildError(
                "bibtex is required when latexmk is unavailable. Install a TeX distribution and retry."
            )
        run_command([bibtex, "report"], latex_root, log_path)
        for _ in range(2):
            run_command(
                [
                    engine,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "submission/report.tex",
                ],
                latex_root,
                log_path,
            )
    else:
        raise BuildError(
            "No LaTeX compiler was found. Install MacTeX or TeX Live with latexmk, "
            "pdflatex, and bibtex, then run make submission again."
        )

    report_path = latex_root / "report.pdf"
    if not report_path.is_file() or report_path.stat().st_size == 0:
        raise BuildError(f"LaTeX completed without a report PDF: {report_path}")
    return report_path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_revision(root: Path) -> str:
    """Return HEAD when available without making Git a build requirement."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="ascii",
            check=False,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def source_worktree_state(root: Path) -> str:
    """Describe whether the package is built from a clean Git worktree."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return "dirty" if result.stdout.strip() else "clean"


def write_manifest(package_root: Path, root: Path) -> Path:
    """Write checksums for every package payload except the manifest itself."""

    manifest = package_root / "manifest.txt"
    payloads = sorted(
        path for path in package_root.rglob("*") if path.is_file() and path != manifest
    )
    lines = [
        "MongoDB consistency submission package",
        f"Source revision: {source_revision(root)}",
        f"Working tree state: {source_worktree_state(root)}",
        "The checksums below identify the packaged source snapshot.",
        "Checksums: SHA-256",
        "",
    ]
    lines.extend(
        f"{sha256(path)}  {path.relative_to(package_root).as_posix()}"
        for path in payloads
    )
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def make_archive(package_root: Path, archive_path: Path) -> None:
    """Create a stable zip archive with sorted entries and fixed timestamps."""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(path for path in package_root.rglob("*") if path.is_file()):
            relative = path.relative_to(package_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def check_documents(root: Path, checker_root: Path) -> None:
    """Run the repository checker against a source or package tree."""

    sys.path.insert(0, str(checker_root))
    try:
        from scripts.check_documentation import check_repository
    except ImportError as error:
        raise BuildError(f"Cannot load the documentation checker: {error}") from error

    files, findings = check_repository(root)
    if findings:
        details = "\n".join(finding.format() for finding in findings)
        raise BuildError(
            f"Documentation check failed for {len(findings)} finding(s) in {len(files)} file(s).\n{details}"
        )


def validate_figure_inputs(root: Path) -> None:
    """Require every report figure input before compiling the PDF."""

    figure_paths: set[str] = set()
    for source_path in (root / "submission").rglob("*.tex"):
        figure_paths.update(
            FIGURE_INPUT_RE.findall(source_path.read_text(encoding="utf-8"))
        )
    missing = sorted(path for path in figure_paths if not (root / path).is_file())
    if missing:
        raise BuildError(
            "Missing report figure input(s): " + ", ".join(missing)
        )


def validate_pdf_text(text: str) -> None:
    """Reject unresolved report placeholders from the compiled PDF text."""

    if "NO DATA:" in text or "NO_DATA" in text:
        raise BuildError(
            "Compiled report contains an unresolved NO DATA placeholder"
        )


def validate_pdf_artifact(path: Path) -> None:
    """Check that a compiled report is readable and contains no placeholders."""

    if not path.is_file() or path.stat().st_size == 0:
        raise BuildError(f"Compiled report PDF is missing or empty: {path}")
    pdftotext = find_tool("pdftotext")
    if not pdftotext:
        raise BuildError(
            "pdftotext is required to verify the compiled report PDF"
        )
    try:
        completed = subprocess.run(
            [pdftotext, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"Could not inspect the compiled report PDF: {error}") from error
    if completed.returncode:
        raise BuildError(
            f"pdftotext failed for the compiled report PDF: {_command_tail(completed.stderr)}"
        )
    validate_pdf_text(completed.stdout)


def build(root: Path) -> tuple[Path, Path, Path]:
    """Build and validate the PDF, archive, and manifest."""

    root = root.resolve()
    validate_layout(root)
    parse_metadata(root / METADATA_PATH)
    validate_package_sources(root)

    output_pdf_dir = root / "output/pdf"
    output_submission_dir = root / "output/submission"
    build_root = root / "tmp/submission-build"
    for path in (output_pdf_dir, output_submission_dir, build_root):
        _remove_generated(path)
        path.mkdir(parents=True, exist_ok=True)

    check_documents(root, root)
    report_path = compile_report(root, build_root)
    validate_pdf_artifact(report_path)

    package_root = build_root / "package"
    package_root.mkdir()
    shutil.copy2(report_path, package_root / "report.pdf")
    package_readme = root / PACKAGE_README_PATH
    shutil.copy2(package_readme, package_root / "README.md")
    shutil.copy2(root / PACKAGE_MAKEFILE_PATH, package_root / "Makefile")
    source_root = package_root / "source"
    copy_source_tree(root, source_root)
    manifest = write_manifest(package_root, root)
    check_documents(package_root, root)

    final_pdf = output_pdf_dir / REPORT_FILENAME
    final_manifest = output_submission_dir / "manifest.txt"
    final_archive = output_submission_dir / ARCHIVE_FILENAME
    shutil.copy2(report_path, final_pdf)
    shutil.copy2(manifest, final_manifest)
    make_archive(package_root, final_archive)
    _remove_generated(build_root)
    return final_pdf, final_archive, final_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: directory containing this script)",
    )
    args = parser.parse_args(argv)
    try:
        pdf, archive, manifest = build(args.root)
    except BuildError as error:
        print(f"Submission build failed: {error}", file=sys.stderr)
        return 1
    print(f"Report: {pdf}")
    print(f"Archive: {archive}")
    print(f"Manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
