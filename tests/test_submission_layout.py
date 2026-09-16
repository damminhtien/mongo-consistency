from __future__ import annotations

import unittest
from pathlib import Path

from scripts.build_submission import (
    METADATA_KEYS,
    REQUIRED_SECTIONS,
    REQUIRED_SUBMISSION_FILES,
    parse_metadata,
)

ROOT = Path(__file__).resolve().parents[1]


class SubmissionLayoutTests(unittest.TestCase):
    def test_required_submission_sources_exist(self) -> None:
        paths = (*REQUIRED_SUBMISSION_FILES, *REQUIRED_SECTIONS)
        missing = [str(path) for path in paths if not (ROOT / path).is_file()]
        self.assertEqual([], missing)

    def test_metadata_has_all_build_fields(self) -> None:
        values = parse_metadata(ROOT / "submission/metadata.mk")
        self.assertEqual(set(METADATA_KEYS), set(values))
        self.assertTrue(all(values[key] for key in METADATA_KEYS))

    def test_makefile_exposes_submission_target(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(".PHONY:", makefile)
        self.assertIn("check-docs", makefile)
        self.assertIn("setup", makefile)
        self.assertIn("submission:", makefile)
        self.assertIn("scripts/build_submission.py", makefile)

    def test_source_manifest_has_explicit_package_roots(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('"configs"', source)
        self.assertIn('"submission"', source)
        self.assertIn('"output"', source)
        self.assertIn('"tmp"', source)

    def test_submission_package_includes_runtime_harness_sources(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        for path in ("compose.yaml", "requirements.txt", "pyproject.toml", "infra"):
            self.assertIn(path, source)


if __name__ == "__main__":
    unittest.main()
