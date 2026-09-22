"""Read immutable provenance recorded beside an extracted submission package."""

from __future__ import annotations

from pathlib import Path


def read_package_manifest(root: Path) -> dict[str, str]:
    """Read the small provenance header from the package manifest, if present."""

    manifest = root.parent / "manifest.txt"
    if not manifest.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in {"Source revision", "Working tree state"}:
            values[key] = value.strip()
    return values


def packaged_revision(root: Path) -> str | None:
    """Return the source revision recorded beside an extracted package."""

    value = read_package_manifest(root).get("Source revision")
    return value if value and value != "unknown" else None


def packaged_tree_is_clean(root: Path) -> bool:
    """Return whether the packaged snapshot was built from a clean tree."""

    return read_package_manifest(root).get("Working tree state") == "clean"
