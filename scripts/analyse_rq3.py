"""Summarize matched RQ3 histories and build the report table and timeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from mongo_consistency.history import read_history

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "results/raw/rq3"
DEFAULT_OUTPUT = ROOT / "results/analysis/rq3"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected an object in {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _history_path(record: dict[str, Any]) -> Path:
    value = Path(str(record["path"]))
    if value.is_absolute() and value.is_file():
        return value
    return ROOT / value


def _operation(history: dict[str, Any], operation_id: str) -> dict[str, Any]:
    return next(
        (
            operation
            for operation in history.get("operations", [])
            if operation.get("operation_id") == operation_id
        ),
        {},
    )


def _initial_topology(history: dict[str, Any]) -> dict[str, Any]:
    checks = history.get("precondition", {}).get("checks", [])
    for check in checks:
        if check.get("name") == "one-primary-two-secondary-topology":
            actual = check.get("actual")
            return actual if isinstance(actual, dict) else {}
    return {}


def _faulted_members(history: dict[str, Any]) -> tuple[str, ...]:
    for event in history.get("fault_events", []):
        if not isinstance(event, dict):
            continue
        if event.get("action") == "isolate":
            members = event.get("members", [])
            if event.get("status") != "APPLIED" or event.get("rules_verified") is not True:
                return ()
            if not isinstance(members, list) or not members:
                return ()
            return tuple(sorted(str(member) for member in members))
    return ()


def _final_members(history: dict[str, Any]) -> dict[str, dict[str, Any]]:
    final = history.get("final_observation")
    members = final.get("members") if isinstance(final, dict) else None
    return members if isinstance(members, dict) else {}


def _final_write_presence(history: dict[str, Any], write_id: str) -> str:
    final = history.get("final_observation")
    topology = final.get("topology") if isinstance(final, dict) else None
    if (
        not isinstance(final, dict)
        or final.get("converged") is not True
        or not isinstance(topology, dict)
        or topology.get("stable") is not True
    ):
        return "unobserved"
    members = _final_members(history)
    if (
        set(members) != {"mongo1", "mongo2", "mongo3"}
        or any(not isinstance(member, dict) for member in members.values())
        or any(
            member.get("reachable") is not True for member in members.values()
        )
    ):
        return "unobserved"
    present = [write_id in member.get("observed_write_ids", []) for member in members.values()]
    if all(present):
        return "all_members"
    if any(present):
        return "some_members"
    return "no_members"


def _matched_topology(pair: dict[str, dict[str, Any]]) -> bool:
    left, right = pair.values()
    left_primary = _initial_topology(left).get("primary")
    right_primary = _initial_topology(right).get("primary")
    left_faulted = _faulted_members(left)
    right_faulted = _faulted_members(right)
    return (
        isinstance(left_primary, str)
        and left_primary in {"mongo1", "mongo2", "mongo3"}
        and left_primary == right_primary
        and bool(left_faulted)
        and left_faulted == right_faulted
    )


def _duration_ms(operation: dict[str, Any]) -> float | None:
    events = operation.get("command_events", [])
    event = events[0] if isinstance(events, list) and events else {}
    start = event.get("started_ns", operation.get("start_ns"))
    end = event.get("end_ns", operation.get("end_ns"))
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return None
    return round((end - start) / 1_000_000, 2)


def _read_signature(history: dict[str, Any]) -> dict[str, Any]:
    operation = _operation(history, "read")
    return {
        "status": operation.get("operation_status"),
        "version": operation.get("observed_version"),
        "route": operation.get("actual_server_address"),
        "after_cluster_time": operation.get("after_cluster_time"),
        "error": operation.get("error_code"),
        "duration_ms": _duration_ms(operation),
        "topology_before": operation.get("topology_before"),
        "topology_after": operation.get("topology_after"),
    }


def _write_signature(history: dict[str, Any], operation_id: str) -> dict[str, Any]:
    operation = _operation(history, operation_id)
    return {
        "status": operation.get("operation_status"),
        "route": operation.get("actual_server_address"),
        "error": operation.get("error_code"),
        "duration_ms": _duration_ms(operation),
        "topology_before": operation.get("topology_before"),
        "topology_after": operation.get("topology_after"),
    }


def _row_for_pair(contrast_id: str, pair_id: str, arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    left_id, right_id = {
        "M1": ("C5", "C6"),
        "M2": ("C8", "C5"),
        "M3": ("C3", "C6"),
    }[contrast_id]
    return {
        "pair_id": pair_id,
        "arms": arms,
        "left_configuration": left_id,
        "right_configuration": right_id,
        "topology_matched": _matched_topology(arms),
    }


def _score_pair(contrast_id: str, pair: dict[str, Any]) -> int:
    arms = pair["arms"]
    left = arms[pair["left_configuration"]]
    right = arms[pair["right_configuration"]]
    score = 2 if pair["topology_matched"] else 0
    if contrast_id == "M1":
        left_read = _read_signature(left)
        right_read = _read_signature(right)
        score += int(left_read["status"] == "SUCCESS" and left_read["version"] == 0)
        score += int(left_read["after_cluster_time"] is None)
        score += int(right_read["after_cluster_time"] is not None)
        score += int(
            right_read["status"] in {"UNAVAILABLE", "INDETERMINATE"}
            or (
                right_read["status"] == "SUCCESS"
                and isinstance(right_read["version"], int)
                and right_read["version"] >= 1
            )
        )
        score += int(
            left_read["route"] is not None and left_read["route"] == right_read["route"]
        )
    elif contrast_id == "M2":
        local_read = _read_signature(left)
        majority_read = _read_signature(right)
        score += int(local_read["status"] == "SUCCESS" and local_read["version"] == 1)
        score += int(majority_read["status"] == "SUCCESS" and majority_read["version"] == 0)
        score += int(
            local_read["route"] is not None and local_read["route"] == majority_read["route"]
        )
    else:
        weak_write = _write_signature(left, "first_write")
        majority_write = _write_signature(right, "first_write")
        score += int(weak_write["status"] == "SUCCESS")
        score += int(majority_write["status"] in {"UNAVAILABLE", "INDETERMINATE"})
        score += int(_final_write_presence(left, "w1") == "no_members")
        score += int(_final_write_presence(right, "w1") == "all_members")
    return score


def _latex_escape(value: Any) -> str:
    text = str(value)
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
    return "".join(replacements.get(character, character) for character in text)


def _history_id(history: dict[str, Any]) -> str:
    return str(history.get("manifest", {}).get("trial_id", "unknown"))


def _member_from_address(address: str | None) -> str:
    return address.split(":", maxsplit=1)[0] if address else "unknown member"


def _format_timestamp(value: Any) -> str:
    if isinstance(value, dict):
        seconds = value.get("seconds")
        increment = value.get("increment")
        if isinstance(seconds, int) and isinstance(increment, int):
            return f"{seconds}:{increment}"
        cluster_time = value.get("cluster_time")
        if isinstance(cluster_time, dict):
            return _format_timestamp(cluster_time)
    return "unavailable"


def _read_text(history: dict[str, Any]) -> str:
    read = _read_signature(history)
    operation = _operation(history, "read")
    events = operation.get("command_events", [])
    command_event = events[0] if isinstance(events, list) and events else {}
    time_bound = read["after_cluster_time"]
    bound_text = (
        "afterClusterTime absent"
        if time_bound is None
        else f"afterClusterTime={_format_timestamp(time_bound)}"
    )
    elapsed = "unavailable" if read["duration_ms"] is None else f"{read['duration_ms']} ms"
    op_time = _format_timestamp(operation.get("operation_time_before"))
    cluster_time = _format_timestamp(operation.get("cluster_time_before"))
    return (
        f"{read['route']}; {command_event.get('command_name')}; "
        f"RC={operation.get('read_concern')}; {bound_text}; "
        f"session opTime={op_time}, clusterTime={cluster_time}; command {elapsed}"
    )


def _read_result_text(history: dict[str, Any]) -> str:
    read = _read_signature(history)
    if read["status"] == "SUCCESS":
        result = f"SUCCESS, v{read['version']}"
    elif read["status"] is None:
        result = "read not reached"
    else:
        result = f"{read['status']}/{read['error'] or 'no error code'}"
    elapsed = "latency unavailable" if read["duration_ms"] is None else f"{read['duration_ms']} ms"
    return f"{result}, {elapsed}"


def _find_pdflatex() -> str | None:
    found = shutil.which("pdflatex")
    if found:
        return found
    candidates: list[Path] = []
    configured = os.environ.get("TEXLIVE_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())
    user_texlive = Path.home() / "texlive"
    if user_texlive.is_dir():
        for version in sorted(user_texlive.iterdir()):
            bin_root = version / "bin"
            if bin_root.is_dir():
                candidates.extend(path for path in sorted(bin_root.iterdir()) if path.is_dir())
    for directory in candidates:
        candidate = directory / "pdflatex"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _render_timeline(pair: dict[str, Any], destination: Path) -> None:
    arms = pair["arms"]
    c5 = arms["C5"]
    c6 = arms["C6"]
    c5_read = _read_signature(c5)
    c6_read = _read_signature(c6)
    c5_write = _write_signature(c5, "write")
    c6_write = _write_signature(c6, "write")
    primary = str(_initial_topology(c5).get("primary", "unobserved"))
    stale_member = _member_from_address(c6_read["route"] or c5_read["route"])
    before = c6_read.get("topology_before") or {}
    target_state = (before.get("members") or {}).get(stale_member, {})
    target_role = target_state.get("role", "unobserved")
    target_last_write = _format_timestamp(target_state.get("last_write_op_time"))
    term = before.get("term")
    term_text = "term unavailable" if term is None else f"term {term}"
    c5_write_op = _operation(c5, "write")
    c6_write_op = _operation(c6, "write")
    write_text = (
        f"Primary {primary}; C5: {c5_write['status']} at "
        f"{_member_from_address(c5_write['route'])}, WC={c5_write_op.get('write_concern')}, "
        f"opTime={_format_timestamp(c5_write_op.get('operation_time_after'))}, "
        f"clusterTime={_format_timestamp(c5_write_op.get('cluster_time_after'))}; "
        f"C6: {c6_write['status']} at {_member_from_address(c6_write['route'])}, "
        f"WC={c6_write_op.get('write_concern')}, "
        f"opTime={_format_timestamp(c6_write_op.get('operation_time_after'))}, "
        f"clusterTime={_format_timestamp(c6_write_op.get('cluster_time_after'))}"
    )
    route_text = (
        f"Target {stale_member}: role={target_role}, lastWrite={target_last_write}, "
        f"{term_text}. C5 read: {_read_text(c5)}. C6 read: {_read_text(c6)}"
    )
    c5_w1 = _final_write_presence(c5, "w1")
    c6_w1 = _final_write_presence(c6, "w1")
    outcome_text = (
        f"C5: {_read_result_text(c5)}; C6: {_read_result_text(c6)}. "
        f"W1 after heal: C5={c5_w1}, C6={c6_w1}."
    )
    source = rf"""\documentclass[10pt]{{article}}
\usepackage[a4paper,margin=1.2cm]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage{{lmodern}}
\usepackage{{booktabs}}
\pagestyle{{empty}}
\setlength{{\parindent}}{{0pt}}
\begin{{document}}
\begin{{center}}
{{\large\bfseries Causal-session contrast: matched seed, separate fault episodes}}\\[0.5em]
\footnotesize\texttt{{{_latex_escape(_history_id(c5))}}}\\
\texttt{{{_latex_escape(_history_id(c6))}}}\\[0.8em]
\begin{{tabular}}{{@{{}}p{{0.27\textwidth}}c p{{0.40\textwidth}}c p{{0.25\textwidth}}@{{}}}}
\toprule
\textbf{{1. Write}} & $\longrightarrow$ & \textbf{{2. Dependent read}} & $\longrightarrow$ & \textbf{{3. Client result}} \\
\midrule
{_latex_escape(write_text)} & $\longrightarrow$ & {_latex_escape(route_text)} & $\longrightarrow$ & {_latex_escape(outcome_text)} \\
\bottomrule
\end{{tabular}}
\end{{center}}
\end{{document}}
"""
    with tempfile.TemporaryDirectory(prefix="rq3-timeline-") as temporary:
        build_dir = Path(temporary)
        source_path = build_dir / "rq3-causal-timeline.tex"
        source_path.write_text(source, encoding="utf-8")
        pdflatex = _find_pdflatex()
        if pdflatex is None:
            raise RuntimeError("pdflatex was not found on PATH, in TEXLIVE_BIN, or under ~/texlive")
        environment = os.environ.copy()
        executable_dir = str(Path(pdflatex).resolve().parent)
        path_entries = environment.get("PATH", "").split(os.pathsep)
        if executable_dir not in path_entries:
            environment["PATH"] = os.pathsep.join(
                (executable_dir, environment.get("PATH", ""))
            )
        result = subprocess.run(
            [
                pdflatex,
                "-halt-on-error",
                "-interaction=nonstopmode",
                "-output-directory",
                str(build_dir),
                str(source_path),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        pdf_path = build_dir / "rq3-causal-timeline.pdf"
        if result.returncode != 0 or not pdf_path.is_file():
            tail = (result.stdout + "\n" + result.stderr)[-5000:]
            raise RuntimeError(f"could not render the RQ3 timeline PDF:\n{tail}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, destination)


def _counts(pairs: list[dict[str, Any]], contrast_id: str) -> dict[str, Any]:
    if contrast_id == "M1":
        return {
            "C5_stale_success_without_after_cluster_time": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C5"])["version"] == 0
                and _read_signature(pair["arms"]["C5"])["after_cluster_time"] is None
                for pair in pairs
            ),
            "C6_after_cluster_time_present": sum(
                _read_signature(pair["arms"]["C6"])["after_cluster_time"] is not None
                for pair in pairs
            ),
            "C6_after_cluster_time_matches_write_time": sum(
                _read_signature(pair["arms"]["C6"])["after_cluster_time"]
                == _operation(pair["arms"]["C6"], "write").get("operation_time_after")
                and _read_signature(pair["arms"]["C6"])["after_cluster_time"] is not None
                for pair in pairs
            ),
            "C6_read_attempted": sum(
                _read_signature(pair["arms"]["C6"])["status"] is not None
                for pair in pairs
            ),
            "C6_successful_read_responses": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                for pair in pairs
            ),
            "C6_successful_reads_without_version": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                and not isinstance(_read_signature(pair["arms"]["C6"])["version"], int)
                for pair in pairs
            ),
            "C6_nonstale_successful_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                and isinstance(_read_signature(pair["arms"]["C6"])["version"], int)
                and _read_signature(pair["arms"]["C6"])["version"] >= 1
                for pair in pairs
            ),
            "C6_stale_successful_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                and isinstance(_read_signature(pair["arms"]["C6"])["version"], int)
                and _read_signature(pair["arms"]["C6"])["version"] < 1
                for pair in pairs
            ),
            "C6_read_not_reached": sum(
                _read_signature(pair["arms"]["C6"])["status"] is None
                for pair in pairs
            ),
        }
    if contrast_id == "M2":
        return {
            "C8_local_read_v1": sum(
                _read_signature(pair["arms"]["C8"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C8"])["version"] == 1
                for pair in pairs
            ),
            "C8_successful_read_responses": sum(
                _read_signature(pair["arms"]["C8"])["status"] == "SUCCESS"
                for pair in pairs
            ),
            "C5_majority_read_v0": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C5"])["version"] == 0
                for pair in pairs
            ),
            "C5_successful_read_responses": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                for pair in pairs
            ),
            "same_route_local_v1_majority_v0": sum(
                _read_signature(pair["arms"]["C8"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C8"])["version"] == 1
                and _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C5"])["version"] == 0
                and _read_signature(pair["arms"]["C8"])["route"]
                == _read_signature(pair["arms"]["C5"])["route"]
                for pair in pairs
            ),
        }
    return {
        "C3_w1_first_write_acknowledged": sum(
            _write_signature(pair["arms"]["C3"], "first_write")["status"] == "SUCCESS"
            for pair in pairs
        ),
        "C3_acknowledged_w1_absent_from_all_converged_members": sum(
            _write_signature(pair["arms"]["C3"], "first_write")["status"] == "SUCCESS"
            and _final_write_presence(pair["arms"]["C3"], "w1") == "no_members"
            for pair in pairs
        ),
        "C3_converged_final_observations": sum(
            _final_write_presence(pair["arms"]["C3"], "w1") != "unobserved"
            for pair in pairs
        ),
        "C6_majority_first_write_acknowledged": sum(
            _write_signature(pair["arms"]["C6"], "first_write")["status"] == "SUCCESS"
            for pair in pairs
        ),
        "C6_majority_first_write_network_timeout": sum(
            _write_signature(pair["arms"]["C6"], "first_write")["error"] == "NetworkTimeout"
            for pair in pairs
        ),
        "C6_w1_present_on_all_final_members_after_timeout": sum(
            _write_signature(pair["arms"]["C6"], "first_write")["error"] == "NetworkTimeout"
            and _final_write_presence(pair["arms"]["C6"], "w1") == "all_members"
            for pair in pairs
        ),
        "C6_converged_final_observations": sum(
            _final_write_presence(pair["arms"]["C6"], "w1") != "unobserved"
            for pair in pairs
        ),
    }


def _summary_and_report(
    *, input_root: Path, output_root: Path, submission_root: Path
) -> dict[str, Any]:
    manifest_path = input_root / "campaign-manifest.json"
    campaign = _read_json(manifest_path)
    if campaign.get("status") != "COMPLETE":
        raise ValueError(f"RQ3 campaign is not complete: {campaign.get('status')}")
    provenance = campaign.get("runtime_provenance")
    if not isinstance(provenance, dict):
        raise TypeError("RQ3 campaign runtime provenance is missing")
    frozen_hashes = {
        "runner_script_sha256": ROOT / "scripts/run_rq3_campaign.py",
        "configuration_sha256": ROOT / "configs/configurations.json",
        "protocol_sha256": ROOT / "docs/experimental-protocol.md",
    }
    for field, path in frozen_hashes.items():
        if campaign.get(field) != _sha256(path):
            raise ValueError(f"RQ3 campaign {field} does not match {path.relative_to(ROOT)}")
    if (
        campaign.get("runner_commit") != provenance.get("runner_commit")
        or provenance.get("runner_dirty") is not False
        or campaign.get("protocol_sha256") != provenance.get("protocol_hash")
        or provenance.get("prediction_manifest_hash") != _sha256(ROOT / "configs/predictions.json")
    ):
        raise ValueError("RQ3 campaign frozen runtime provenance does not match its manifest")
    repetitions = campaign.get("repetitions_per_contrast")
    if type(repetitions) is not int or not 5 <= repetitions <= 10:
        raise ValueError("RQ3 campaign repetitions must be between five and ten")
    expected_count = campaign.get("planned_case_count")
    records = campaign.get("records", [])
    if (
        expected_count != repetitions * 6
        or campaign.get("completed_case_count") != expected_count
        or not isinstance(records, list)
        or len(records) != expected_count
    ):
        raise ValueError("RQ3 campaign manifest does not contain every planned history")

    contrast_specs = {
        "M1": ("rq3-m1", {"C5", "C6"}, "RYW", "m1"),
        "M2": ("rq3-m2", {"C8", "C5"}, "WFR", "m2"),
        "M3": ("rq3-m3", {"C3", "C6"}, "MW", "m3"),
    }
    capture_operations = {
        "M1": ["write", "read"],
        "M2": ["read", "write"],
        "M3": ["first_write", "second_write"],
    }
    first_operations = {"M1": "write", "M2": "read", "M3": "first_write"}
    followup_operations = {"M1": "read", "M2": "write", "M3": "second_write"}
    records_by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    pair_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_trials: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("RQ3 campaign contains a non-object record")
        contrast_id = record.get("contrast_id")
        spec = contrast_specs.get(contrast_id) if isinstance(contrast_id, str) else None
        if spec is None:
            raise ValueError(f"unknown RQ3 contrast: {contrast_id!r}")
        campaign_id, expected_arms, property_name, pair_prefix = spec
        configuration_id = record.get("configuration_id")
        pair_id = record.get("pair_id")
        replicate = record.get("replicate")
        trial_id = record.get("trial_id")
        pair_seed = record.get("pair_seed")
        if (
            not isinstance(configuration_id, str)
            or configuration_id not in expected_arms
            or record.get("campaign") != campaign_id
            or record.get("property") != property_name
            or record.get("runner_commit") != campaign.get("runner_commit")
            or type(replicate) is not int
            or not 1 <= replicate <= repetitions
            or pair_id != f"{pair_prefix}-r{replicate:02d}"
            or type(pair_seed) is not int
            or record.get("seed") != pair_seed
            or not isinstance(trial_id, str)
            or not trial_id
            or trial_id in seen_trials
        ):
            raise ValueError(f"invalid RQ3 matched-pair identity for {trial_id!r}")
        seen_trials.add(trial_id)
        path = _history_path(record)
        history_object = read_history(path)
        history = history_object.to_dict()
        if history_object.history_hash != record.get("history_hash"):
            raise ValueError(f"history hash differs from the RQ3 campaign manifest: {path}")
        history_manifest = history.get("manifest", {})
        expected_fields = {
            "trial_id": trial_id,
            "campaign_id": campaign_id,
            "configuration_id": configuration_id,
            "property": property_name,
            "seed": pair_seed,
            "rq3_contrast_id": contrast_id,
            "rq3_pair_id": pair_id,
            "rq3_pair_seed": pair_seed,
            "rq3_replicate": replicate,
        }
        if any(history_manifest.get(key) != value for key, value in expected_fields.items()):
            raise ValueError(f"history identity differs from its campaign record: {path}")
        if (
            history_manifest.get("runner_commit") != campaign.get("runner_commit")
            or history_manifest.get("runner_dirty") is not False
            or not isinstance(provenance, dict)
            or history_manifest.get("software_versions") != provenance.get("software_versions")
            or history_manifest.get("image_digest") != provenance.get("image_digest")
            or history_manifest.get("checker_version") != provenance.get("checker_version")
            or any(
                history_manifest.get(key) != provenance.get(key)
                for key in (
                    "protocol_commit",
                    "protocol_hash",
                    "prediction_commit",
                    "prediction_manifest_hash",
                )
            )
        ):
            raise ValueError(f"history provenance differs from the RQ3 campaign: {path}")
        if record.get("outcome") == "HARNESS_ERROR" or record.get("runner_error") is not None:
            raise ValueError(f"harness-error history cannot produce RQ3 report evidence: {path}")

        operations = {
            operation.get("operation_id"): operation
            for operation in history.get("operations", [])
            if isinstance(operation, dict) and isinstance(operation.get("operation_id"), str)
        }
        if history_manifest.get("topology_capture_operations") != capture_operations[contrast_id]:
            raise ValueError(f"history topology capture plan differs from {contrast_id}: {path}")
        precondition = history.get("precondition", {})
        precondition_status = precondition.get("status") if isinstance(precondition, dict) else None
        schedule_outcome = history_manifest.get("schedule_outcome")
        for operation_id in capture_operations[contrast_id]:
            operation = operations.get(operation_id)
            if operation is None:
                allowed = precondition_status == "PRECONDITION_MISS"
                if operation_id == followup_operations[contrast_id]:
                    preceding = operations.get(first_operations[contrast_id])
                    allowed = allowed or (
                        preceding is not None
                        and preceding.get("operation_status")
                        in {"UNAVAILABLE", "INDETERMINATE", "HARNESS_ERROR"}
                    )
                    allowed = allowed or (
                        isinstance(schedule_outcome, dict)
                        and schedule_outcome.get("outcome") in {"UNAVAILABLE", "INDETERMINATE"}
                    )
                if not allowed:
                    raise ValueError(f"required RQ3 operation {operation_id} is missing: {path}")
                continue
            if not isinstance(operation.get("topology_before"), dict) or not isinstance(
                operation.get("topology_after"), dict
            ):
                raise TypeError(f"RQ3 operation {operation_id} is missing topology snapshots: {path}")
        history["result"] = record.get("outcome")
        history["source_path"] = path.relative_to(ROOT).as_posix()
        if configuration_id in records_by_pair[pair_id]:
            raise ValueError(f"{pair_id} contains a duplicate configuration arm")
        records_by_pair[pair_id][configuration_id] = history
        pair_records[pair_id].append(record)

    expected_pair_ids = {
        f"{contrast_id.lower()}-r{replicate:02d}"
        for contrast_id in contrast_specs
        for replicate in range(1, repetitions + 1)
    }
    if set(records_by_pair) != expected_pair_ids:
        raise ValueError("RQ3 campaign has missing or unexpected pair IDs")
    for pair_id, items in pair_records.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        expected_arms = contrast_specs[contrast_id][1]
        seeds = [item.get("pair_seed") for item in items]
        configurations = [item.get("configuration_id") for item in items]
        if (
            len(items) != 2
            or set(configurations) != expected_arms
            or any(type(seed) is not int for seed in seeds)
            or seeds[0] != seeds[1]
        ):
            raise ValueError(f"{pair_id} does not contain the registered matched-seed arms")

    grouped: dict[str, list[dict[str, Any]]] = {"M1": [], "M2": [], "M3": []}
    for pair_id, arms in records_by_pair.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        expected_arms = contrast_specs[contrast_id][1]
        if set(arms) != expected_arms:
            raise ValueError(f"{pair_id} does not contain the registered configuration pair")
        grouped[contrast_id].append(_row_for_pair(contrast_id, pair_id, arms))

    selections: dict[str, Any] = {}
    summary_contrasts: dict[str, Any] = {}
    for contrast_id, pairs in grouped.items():
        pairs.sort(key=lambda pair: pair["pair_id"])
        if len(pairs) != campaign.get("repetitions_per_contrast"):
            raise ValueError(f"{contrast_id} is missing matched pairs")
        chosen = min(
            pairs,
            key=lambda pair: (-_score_pair(contrast_id, pair), pair["pair_id"]),
        )
        selections[contrast_id] = {
            "pair_id": chosen["pair_id"],
            "topology_matched": chosen["topology_matched"],
            "score": _score_pair(contrast_id, chosen),
            "histories": [
                {
                    "trial_id": _history_id(history),
                    "path": history["source_path"],
                    "sha256": history["history_hash"],
                    "configuration_id": history["manifest"]["configuration_id"],
                }
                for history in chosen["arms"].values()
            ],
        }
        summary_contrasts[contrast_id] = {
            "pair_count": len(pairs),
            "topology_matched_pairs": sum(pair["topology_matched"] for pair in pairs),
            "signature_counts": _counts(pairs, contrast_id),
            "selected_pair": selections[contrast_id],
        }

    causal_pair = next(
        pair
        for pair in grouped["M1"]
        if pair["pair_id"] == selections["M1"]["pair_id"]
    )
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    output_root.mkdir(parents=True, exist_ok=True)
    selection_manifest = {
        "schema_version": "rq3-selection.v1",
        "campaign_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "campaign_manifest_sha256": manifest_digest,
        "selected_pairs": selections,
    }
    (output_root / "selection-manifest.json").write_text(
        json.dumps(selection_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    generated_section = _render_report_section(grouped, selections, campaign)
    section_path = submission_root / "generated-rq3.tex"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(generated_section, encoding="utf-8")
    _render_timeline(
        causal_pair,
        submission_root / "figures/rq3-causal-timeline.pdf",
    )
    generated_artifacts = {
        "report_tex": {
            "path": section_path.relative_to(ROOT).as_posix(),
            "sha256": _sha256(section_path),
        },
        "timeline_pdf": {
            "path": (submission_root / "figures/rq3-causal-timeline.pdf")
            .relative_to(ROOT)
            .as_posix(),
            "sha256": _sha256(submission_root / "figures/rq3-causal-timeline.pdf"),
        },
    }
    summary = {
        "schema_version": "rq3-analysis.v1",
        "campaign_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "campaign_manifest_sha256": manifest_digest,
        "repetitions_per_contrast": campaign["repetitions_per_contrast"],
        "contrast_summaries": summary_contrasts,
        "selection_manifest": "results/analysis/rq3/selection-manifest.json",
        "generated_artifacts": generated_artifacts,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _render_report_section(
    grouped: dict[str, list[dict[str, Any]]],
    selections: dict[str, Any],
    campaign: dict[str, Any],
) -> str:
    repetitions = int(campaign["repetitions_per_contrast"])
    specifications = {
        "M1": ("C5/C6", "causal session off/on", "C5 stale v0 without a causal time bound", "C6 carries afterClusterTime without a stale successful response"),
        "M2": ("C8/C5", "read concern local/majority", "C8 local read returns v1", "C5 majority read returns v0"),
        "M3": ("C3/C6", "write concern w:1/majority", "C3 first write is acknowledged", "C6 majority first write acknowledgement/status and final W1 state"),
    }
    rows = []
    for contrast_id in ("M1", "M2", "M3"):
        pairs = grouped[contrast_id]
        counts = _counts(pairs, contrast_id)
        left, changed, _, _ = specifications[contrast_id]
        n = repetitions
        if contrast_id == "M1":
            evidence = (
                f"C5 stale v0/no bound: {counts['C5_stale_success_without_after_cluster_time']}/{n}; "
                f"C6 afterClusterTime present/matches W1 operationTime: "
                f"{counts['C6_after_cluster_time_present']}/{n}/"
                f"{counts['C6_after_cluster_time_matches_write_time']}/{n}; "
                f"C6 reads issued: {counts['C6_read_attempted']}/{n}, "
                f"successful: {counts['C6_successful_read_responses']}/{n}, "
                f"not reached: {counts['C6_read_not_reached']}/{n}; "
                f"successful without a version: {counts['C6_successful_reads_without_version']}; "
                f"nonstale concrete values: {counts['C6_nonstale_successful_reads']}/"
                f"{counts['C6_successful_read_responses']}; "
                f"stale among successful: {counts['C6_stale_successful_reads']}/"
                f"{counts['C6_successful_read_responses']}."
            )
        elif contrast_id == "M2":
            evidence = (
                f"C8 local v1: {counts['C8_local_read_v1']}/{n} "
                f"(successful reads: {counts['C8_successful_read_responses']}/{n}); "
                f"C5 majority v0: {counts['C5_majority_read_v0']}/{n} "
                f"(successful reads: {counts['C5_successful_read_responses']}/{n}); "
                f"same-route differential: {counts['same_route_local_v1_majority_v0']}/{n}."
            )
        else:
            evidence = (
                f"C3 w:1 first-write acknowledgements: {counts['C3_w1_first_write_acknowledged']}/{n}; "
                f"acknowledged W1 absent from every converged post-heal member: "
                f"{counts['C3_acknowledged_w1_absent_from_all_converged_members']}/{n}; "
                f"C3 converged final states: {counts['C3_converged_final_observations']}/{n}; "
                f"C6 majority acknowledgements: {counts['C6_majority_first_write_acknowledged']}/{n}; "
                f"C6 majority timeouts: {counts['C6_majority_first_write_network_timeout']}/{n}; "
                f"W1 later present on all converged members after NetworkTimeout: "
                f"{counts['C6_w1_present_on_all_final_members_after_timeout']}/{n}; "
                f"C6 converged final states: {counts['C6_converged_final_observations']}/{n}."
            )
        topology_matched = sum(pair["topology_matched"] for pair in pairs)
        evidence += f" Starting primary and fault target matched: {topology_matched}/{n}."
        chosen = selections[contrast_id]
        ids = "/".join(_latex_escape(item["trial_id"]) for item in chosen["histories"])
        matched = "topology matched" if chosen["topology_matched"] else "topology differed"
        rows.append(
            f"{contrast_id} ({_latex_escape(left)}) & {_latex_escape(changed)} & "
            f"{_latex_escape(evidence)} & {_latex_escape(matched)}; "
            f"exemplar \\texttt{{{ids}}} \\\\"
        )
    return rf"""\subsection{{Mechanism contrasts (RQ3)}}

The study replays {repetitions} matched-seed pairs per contrast. Each arm is a
separate history and fault episode, so the seed and schedule are matched while
the physical injections are independent. The counts below include every
planned pair; topology matches are reported explicitly. The term and role
snapshots are direct observations \cite{{mongodb-replset-status}}. The
interpretations are consistent with documented causal-session, read-concern,
and write-concern behavior
\cite{{mongodb-causal-consistency,mongodb-read-concern,mongodb-write-concern}};
they do not expose MongoDB's internal wait or replication state.

{{\scriptsize
\setlength{{\tabcolsep}}{{3pt}}
\begin{{longtable}}{{@{{}}p{{1.8cm}}p{{2.8cm}}p{{7.8cm}}p{{2.1cm}}@{{}}}}
\caption{{RQ3 matched-seed mechanism contrasts. Counts use all {repetitions} pairs per contrast.}}
\label{{tab:rq3-mechanisms}}\\
\toprule
Pair & Changed factor & Observed signatures & Topology / exemplar \\
\midrule
\endfirsthead
\caption[]{{RQ3 matched-seed mechanism contrasts (continued).}}\\
\toprule
Pair & Changed factor & Observed signatures & Topology / exemplar \\
\midrule
\endhead
\bottomrule
\endfoot
{'\n'.join(rows)}
\end{{longtable}}
}}

The C3/C6 write comparison distinguishes acknowledgement from effect: a
\texttt{{w:1}} acknowledgement followed by W1 missing from every converged,
post-heal member is an observed rollback; a majority-write timeout remains
unresolved until the direct final-state observation. The C5/C6 causal comparison records
the command's causal time bound and client response separately, so a timeout is
not described as proof of a server-side wait. Read concern is interpreted from
the value and route actually recorded for each WFR read.

\maybefigure[fig:rq3-causal-timeline]{{submission/figures/rq3-causal-timeline.pdf}}{{Matched-seed C5/C6 RYW trace. The two arms use independent fault episodes; client command metadata, direct topology snapshots, and response outcomes are shown from the selected histories.}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--submission-root", type=Path, default=ROOT / "submission")
    args = parser.parse_args()
    summary = _summary_and_report(
        input_root=args.input_root,
        output_root=args.output_root,
        submission_root=args.submission_root,
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
