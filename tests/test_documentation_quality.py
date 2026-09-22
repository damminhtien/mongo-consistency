from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_documentation import (
    DOCUMENT_SUFFIXES,
    check_repository,
    check_text,
)

ROOT = Path(__file__).resolve().parents[1]


class DocumentationQualityTests(unittest.TestCase):
    def test_repository_documents_pass(self) -> None:
        files, findings = check_repository(ROOT)
        self.assertGreaterEqual(len(files), 6)
        self.assertEqual(
            [], findings, "\n".join(finding.format() for finding in findings)
        )

    def test_documentation_checker_has_no_latex_or_pdf_input_contract(self) -> None:
        self.assertNotIn(".tex", DOCUMENT_SUFFIXES)
        self.assertNotIn(".pdf", DOCUMENT_SUFFIXES)

    def test_stock_language_is_reported(self) -> None:
        findings = check_text("sample.md", "This document aims to explain the result.")
        self.assertIn("stock-language", {finding.rule for finding in findings})

    def test_vague_language_is_reported(self) -> None:
        findings = check_text("sample.md", "The robust design is comprehensive.")
        self.assertIn("vague-language", {finding.rule for finding in findings})

    def test_unqualified_claim_is_reported(self) -> None:
        findings = check_text("sample.md", "The experiment proves the system is safe.")
        self.assertIn("unsupported-claim", {finding.rule for finding in findings})

    def test_scoped_claim_is_allowed(self) -> None:
        findings = check_text(
            "sample.md",
            "No observed violation is a universal guarantee; it is limited to the tested schedule.",
        )
        self.assertNotIn("unsupported-claim", {finding.rule for finding in findings})

    def test_decorative_punctuation_is_reported(self) -> None:
        findings = check_text("sample.md", "Use an em dash — or an arrow → here.")
        self.assertIn("decorative-punctuation", {finding.rule for finding in findings})

    def test_control_characters_and_duplicate_paragraphs_are_reported(self) -> None:
        paragraph = "The trial record must identify the workload, fault schedule, and measured outcome."
        findings = check_text(
            "sample.md",
            f"{paragraph}\n\n{paragraph}\n\nNull byte: \x00\n",
        )
        rules = {finding.rule for finding in findings}
        self.assertTrue({"control-character", "duplicate-paragraph"} <= rules)

    def test_placeholders_empty_links_and_duplicate_headings_are_reported(self) -> None:
        text = "# Results\n\nFIXME: add data.\n\n[source]()\n\n# Results\n"
        findings = check_text("sample.md", text)
        rules = {finding.rule for finding in findings}
        self.assertTrue({"placeholder", "empty-link", "duplicate-heading"} <= rules)

    def test_repeated_headings_in_separate_sections_are_allowed(self) -> None:
        text = (
            "# Results\n\n"
            "## RYW\n\n### Prediction\n\n"
            "## MR\n\n### Prediction\n"
        )
        findings = check_text("sample.md", text)
        self.assertNotIn("duplicate-heading", {finding.rule for finding in findings})


if __name__ == "__main__":
    unittest.main()
