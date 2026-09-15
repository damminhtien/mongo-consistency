from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.check_documentation import (
    DOCUMENT_SUFFIXES,
    check_repository,
    check_text,
)

ROOT = Path(__file__).resolve().parents[1]


def _minimal_pdf() -> bytes:
    content = b"BT\n/F1 12 Tf\n72 720 Td\n(Plain report text.) Tj\nET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        b"trailer\n<< /Root 1 0 R /Size 6 >>\n"
        + f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


class DocumentationQualityTests(unittest.TestCase):
    def test_repository_documents_pass(self) -> None:
        files, findings = check_repository(ROOT)
        self.assertGreaterEqual(len(files), 6)
        self.assertEqual(
            [], findings, "\n".join(finding.format() for finding in findings)
        )

    def test_pdf_is_a_scanned_document_type(self) -> None:
        self.assertIn(".pdf", DOCUMENT_SUFFIXES)

    def test_agent_metadata_is_not_scanned(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "A short project document.\n", encoding="utf-8"
            )
            (root / "AGENTS.md").write_text(
                "This document should stay local.\n", encoding="utf-8"
            )
            (root / ".codex").mkdir()
            (root / ".codex" / "notes.md").write_text(
                "This document should stay local.\n", encoding="utf-8"
            )
            files, findings = check_repository(root)
        self.assertEqual(["README.md"], [path.name for path in files])
        self.assertEqual([], findings)

    @unittest.skipUnless(
        shutil.which("pdfinfo") and shutil.which("pdftotext"),
        "Poppler is required for the PDF integration check",
    )
    def test_valid_pdf_is_extracted_and_checked(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "submission.pdf"
            pdf.write_bytes(_minimal_pdf())
            files, findings = check_repository(root)
        self.assertEqual([pdf.resolve()], files)
        self.assertEqual(
            [], findings, "\n".join(finding.format() for finding in findings)
        )

    def test_latex_bullet_is_checked_but_pdf_item_bullet_is_allowed(self) -> None:
        source_findings = check_text("report.tex", "\\section{Results}\n• item")
        pdf_findings = check_text("submission.pdf", "• item", is_pdf=True)
        self.assertIn(
            "decorative-punctuation", {finding.rule for finding in source_findings}
        )
        self.assertNotIn(
            "decorative-punctuation", {finding.rule for finding in pdf_findings}
        )

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


if __name__ == "__main__":
    unittest.main()
