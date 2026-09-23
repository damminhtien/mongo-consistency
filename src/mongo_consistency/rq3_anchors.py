"""Load and verify the historical RQ1 traces used to frame RQ3."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .history import read_history

ANCHOR_MANIFEST = Path("configs/rq3-anchors.json")
EXPECTED_ANCHORS = {
    "M1": ("RYW", ("C5", "C6")),
    "M2": ("WFR", ("C8", "C5")),
    "M3": ("MW", ("C3", "C6")),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_anchor_manifest(repository_root: Path) -> tuple[dict[str, Any], str]:
    """Verify the six selected historical records and return manifest plus digest."""

    root = repository_root.resolve()
    manifest_path = root / ANCHOR_MANIFEST
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "rq3-anchor-selection.v1":
        raise ValueError("RQ3 anchor manifest must use rq3-anchor-selection.v1")
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != len(EXPECTED_ANCHORS):
        raise ValueError("RQ3 anchor manifest must contain exactly three pairs")

    seen: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise TypeError("RQ3 anchor manifest contains a non-object pair")
        contrast_id = pair.get("contrast_id")
        expected = EXPECTED_ANCHORS.get(contrast_id) if isinstance(contrast_id, str) else None
        if expected is None or contrast_id in seen:
            raise ValueError(f"RQ3 anchor manifest has an unknown or duplicate contrast: {contrast_id!r}")
        seen.add(contrast_id)
        property_name, configurations = expected
        if pair.get("property") != property_name or pair.get("configurations") != list(configurations):
            raise ValueError(f"RQ3 {contrast_id} anchor pair has an invalid property or configuration order")
        histories = pair.get("histories")
        if not isinstance(histories, list) or len(histories) != 2:
            raise ValueError(f"RQ3 {contrast_id} anchor pair must contain two histories")
        if tuple(item.get("configuration_id") for item in histories if isinstance(item, dict)) != configurations:
            raise ValueError(f"RQ3 {contrast_id} anchor arms do not match the registered configuration order")

        for record in histories:
            if not isinstance(record, dict):
                raise TypeError(f"RQ3 {contrast_id} anchor history must be an object")
            relative_path = record.get("path")
            if not isinstance(relative_path, str) or Path(relative_path).is_absolute():
                raise ValueError(f"RQ3 {contrast_id} anchor path must be repository-relative")
            path = (root / relative_path).resolve()
            try:
                path.relative_to((root / "results/raw/rq3-historical-anchors").resolve())
            except ValueError as error:
                raise ValueError(
                    f"RQ3 anchor path escapes the historical anchor archive: {relative_path}"
                ) from error
            if not path.is_file() or sha256_file(path) != record.get("raw_sha256"):
                raise ValueError(f"RQ3 anchor raw-file digest differs: {relative_path}")
            history = read_history(path)
            manifest = history.manifest
            expected_history = {
                "trial_id": record.get("trial_id"),
                "configuration_id": record.get("configuration_id"),
                "property": property_name,
                "seed": record.get("seed"),
            }
            if history.history_hash != record.get("history_hash") or any(
                manifest.get(key) != value for key, value in expected_history.items()
            ):
                raise ValueError(f"RQ3 anchor identity or history digest differs: {relative_path}")

    if seen != set(EXPECTED_ANCHORS):
        raise ValueError("RQ3 anchor manifest does not cover M1, M2, and M3")
    return payload, sha256_file(manifest_path)
