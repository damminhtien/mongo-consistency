"""Validate the PDF and archive produced by make submission."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from build_submission import (
    ARCHIVE_FILENAME,
    REPORT_FILENAME,
    BuildError,
    validate_figure_inputs,
    validate_pdf_artifact,
)

ROOT = Path(__file__).resolve().parents[1]


def check_artifacts(root: Path = ROOT) -> list[str]:
    """Return submission artifact failures without rebuilding the report."""

    errors: list[str] = []
    try:
        validate_figure_inputs(root)
    except BuildError as error:
        errors.append(str(error))

    pdf_path = root / "output/pdf" / REPORT_FILENAME
    try:
        validate_pdf_artifact(pdf_path)
    except BuildError as error:
        errors.append(str(error))

    archive_path = root / "output/submission" / ARCHIVE_FILENAME
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        errors.append(f"Submission archive is missing or empty: {archive_path}")
    else:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                broken = archive.testzip()
                if broken is not None:
                    errors.append(f"Submission archive has a corrupt entry: {broken}")
                names = set(archive.namelist())
                for required in ("report.pdf", "README.md", "manifest.txt"):
                    if required not in names:
                        errors.append(f"Submission archive is missing {required}")
        except (OSError, zipfile.BadZipFile) as error:
            errors.append(f"Cannot inspect submission archive: {error}")

    manifest = root / "output/submission/manifest.txt"
    if not manifest.is_file() or manifest.stat().st_size == 0:
        errors.append(f"Submission manifest is missing or empty: {manifest}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors = check_artifacts(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Submission artifacts passed: PDF, archive, manifest, and figure inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
