"""Check repository documentation for avoidable boilerplate and weak claims.

The checker is deliberately deterministic. It reviews prose and document
structure; it does not try to identify who wrote a document.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DOCUMENT_SUFFIXES = frozenset(
    {
        ".adoc",
        ".asciidoc",
        ".bib",
        ".cls",
        ".html",
        ".htm",
        ".ltx",
        ".markdown",
        ".md",
        ".mdx",
        ".org",
        ".pdf",
        ".rst",
        ".tex",
        ".text",
        ".textile",
        ".txt",
    }
)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "graphify-out",
        "node_modules",
        "venv",
        ".venv",
    }
)

DECORATIVE_CHARS = {
    "—": "em dash",
    "–": "en dash",
    "‑": "non-breaking hyphen",
    "→": "right arrow",
    "←": "left arrow",
    "“": "left curly quote",
    "”": "right curly quote",
    "‘": "left curly apostrophe",
    "’": "right curly apostrophe",
    "…": "ellipsis",
    "•": "bullet character",
}
DECORATIVE_CHAR_SET = frozenset(DECORATIVE_CHARS)

# A normal LaTeX itemize environment produces a bullet in extracted PDF text.
# Keep that expected output valid while still rejecting the other decorative
# characters in the submitted PDF.
PDF_ALLOWED_DECORATIVE_CHARS = frozenset({"•"})

STOCK_PATTERNS = (
    (r"\bthis\s+(?:document|report|repository)\b", "stock self-reference"),
    (
        r"\bthe\s+purpose\s+of\s+this\s+(?:document|report)\b",
        "generic purpose statement",
    ),
    (
        r"\bthis\s+(?:document|report)\s+(?:aims|seeks)\s+to\b",
        "generic aim statement",
    ),
    (r"\bit\s+is\s+important\s+to\s+note\b", "stock transition"),
    (r"\bit\s+should\s+be\s+noted\b", "stock transition"),
    (r"\bkey\s+takeaways?\b", "stock summary label"),
    (r"\bbest\s+practices?\b", "generic advice label"),
    (r"\bmoving\s+forward\b", "stock transition"),
    (r"\bat\s+the\s+end\s+of\s+the\s+day\b", "stock transition"),
    (r"\bevidence\s+chain\b", "template provenance label"),
    (r"\bdefinition\s+of\s+done\b", "template process label"),
    (r"\bstatus\s+and\s+provenance\b", "template status label"),
    (r"\bparking\s+lot\b", "template planning label"),
    (r"\bat\s+a\s+high\s+level\b", "generic abstraction"),
    (r"\bit\s+is\s+worth\s+noting\b", "stock transition"),
    (r"\bin\s+order\s+to\b", "wordy stock phrase"),
)

VAGUE_PATTERNS = (
    "robust",
    "seamless",
    "comprehensive",
    "meaningful",
    "appropriate",
    "crucial",
    "pivotal",
    "innovative",
    "holistic",
    "multifaceted",
    "utilize",
    "leverage",
    "leveraging",
    "delve",
    "cutting-edge",
    "state-of-the-art",
    "best-in-class",
    "world-class",
    "groundbreaking",
    "revolutionary",
    "transformative",
)

UNSUPPORTED_CLAIM_PATTERNS = (
    (
        (
            r"\b(?:the|this|our)\s+(?:experiment|experiments|study|results?|data|analysis|system)\s+"
            r"(?:prove|proves|guarantee|guarantees|establish|establishes|demonstrate|demonstrates)\b"
        ),
        "unqualified result claim",
    ),
    (r"\b(?:proven|guaranteed)\s+(?:to|that)\b", "unqualified certainty"),
)

PLACEHOLDER_PATTERNS = (
    (r"\bFIXME\b", "FIXME marker"),
    (r"\bTBD\b", "TBD marker"),
    (r"\blorem\s+ipsum\b", "placeholder text"),
    (r"\bcitation\s+needed\b", "missing citation marker"),
    (r"<\s*insert\b[^>]*>", "insertion placeholder"),
    (r"\bTODO\b(?!\.md\b)", "TODO marker"),
)

EMPTY_LINK_RE = re.compile(r"\[[^\]\n]+\]\(\s*\)")
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
MARKDOWN_HEADING_LEVEL_RE = re.compile(r"^\s{0,3}(#{1,6})\s+")
HTML_HEADING_RE = re.compile(r"^\s*<h[1-6][^>]*>(.*?)</h[1-6]>\s*$", re.IGNORECASE)
HTML_HEADING_LEVEL_RE = re.compile(r"^\s*<h([1-6])\b", re.IGNORECASE)
LATEX_HEADING_RE = re.compile(
    r"^\s*\\(?:chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\{([^}]*)\}"
)
LATEX_HEADING_LEVELS = {
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
LATEX_HEADING_LEVEL_RE = re.compile(
    r"^\s*\\(chapter|section|subsection|subsubsection|paragraph|subparagraph)"
)


@dataclass(frozen=True)
class Finding:
    """One actionable documentation finding."""

    path: str
    line: int
    rule: str
    message: str
    excerpt: str

    def format(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location} [{self.rule}] {self.message}\n    {self.excerpt}"


def _tracked_files(root: Path) -> set[Path] | None:
    """Return cached Git paths when the scan root is a project checkout."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return {
        root / Path(value)
        for value in result.stdout.decode("utf-8", errors="replace").split("\0")
        if value
    }


def iter_document_files(root: Path) -> Iterable[Path]:
    """Yield document files that belong to the project source."""

    root = root.resolve()
    tracked = _tracked_files(root)
    if tracked is not None:
        for path in sorted(tracked):
            if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES:
                yield path
        return
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if dirname not in SKIP_DIRS and not dirname.startswith(".")
        )
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.suffix.lower() in DOCUMENT_SUFFIXES:
                yield path


def _relative_path(path: Path, root: Path | None = None) -> str:
    if root is None:
        return path.as_posix()
    return path.resolve().relative_to(root.resolve()).as_posix()


def _excerpt(line: str) -> str:
    compact = " ".join(line.strip().split())
    return compact[:160] or "<blank line>"


def _finding(
    path: str, line: int, rule: str, message: str, source_line: str
) -> Finding:
    return Finding(path, line, rule, message, _excerpt(source_line))


def _prose_lines(text: str) -> tuple[list[tuple[int, str]], bool]:
    """Return non-code-fence lines and whether a fence was left open."""

    prose: list[tuple[int, str]] = []
    in_fence = False
    fence_re = re.compile(r"^\s*(```|~~~)")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if fence_re.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            prose.append((line_number, line))
    return prose, in_fence


def _normalise_heading(value: str) -> str:
    value = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^]]*\])?\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[`*_{}]", "", value)
    return " ".join(value.split()).strip().casefold()


def _heading_matches(path: str, line: str) -> re.Match[str] | None:
    suffix = Path(path).suffix.lower()
    if suffix in {".tex", ".ltx"}:
        return LATEX_HEADING_RE.match(line)
    if suffix in {".html", ".htm"}:
        return HTML_HEADING_RE.match(line)
    return MARKDOWN_HEADING_RE.match(line)


def _heading_level(path: str, line: str) -> int:
    suffix = Path(path).suffix.lower()
    if suffix in {".tex", ".ltx"}:
        match = LATEX_HEADING_LEVEL_RE.match(line)
        if match:
            return LATEX_HEADING_LEVELS[match.group(1)]
    elif suffix in {".html", ".htm"}:
        match = HTML_HEADING_LEVEL_RE.match(line)
        if match:
            return int(match.group(1))
    else:
        match = MARKDOWN_HEADING_LEVEL_RE.match(line)
        if match:
            return len(match.group(1))
    raise ValueError(f"could not determine heading level for {path!r}: {line!r}")


def check_text(path: str, text: str, *, is_pdf: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    prose_lines, unclosed_fence = _prose_lines(text)

    if unclosed_fence:
        findings.append(
            _finding(
                path,
                len(lines) or 1,
                "structure",
                "unclosed Markdown code fence",
                lines[-1] if lines else "",
            )
        )

    allowed_decorative = PDF_ALLOWED_DECORATIVE_CHARS if is_pdf else frozenset()
    for line_number, line in enumerate(lines, start=1):
        for character in sorted(set(line) & DECORATIVE_CHAR_SET):
            if character in allowed_decorative:
                continue
            findings.append(
                _finding(
                    path,
                    line_number,
                    "decorative-punctuation",
                    f"replace {DECORATIVE_CHARS[character]} with plain text or LaTeX syntax",
                    line,
                )
            )
        for character in line:
            if ord(character) < 32 and character not in "\t\r\n\f":
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "control-character",
                        f"remove control character U+{ord(character):04X}",
                        line,
                    )
                )

    for line_number, line in prose_lines:
        for pattern, description in STOCK_PATTERNS:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "stock-language",
                        f"remove or rewrite {description}: {match.group(0)!r}",
                        line,
                    )
                )

        for word in VAGUE_PATTERNS:
            match = re.search(
                rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])",
                line,
                re.IGNORECASE,
            )
            if match:
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "vague-language",
                        f"replace vague wording with a measurable condition: {match.group(0)!r}",
                        line,
                    )
                )

        for pattern, description in UNSUPPORTED_CLAIM_PATTERNS:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "unsupported-claim",
                        f"qualify {description} with scope and evidence: {match.group(0)!r}",
                        line,
                    )
                )

        for pattern, description in PLACEHOLDER_PATTERNS:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "placeholder",
                        f"remove {description}: {match.group(0)!r}",
                        line,
                    )
                )

        match = EMPTY_LINK_RE.search(line)
        if match:
            findings.append(
                _finding(
                    path,
                    line_number,
                    "empty-link",
                    f"add a destination to Markdown link {match.group(0)!r}",
                    line,
                )
            )

    if not is_pdf:
        headings: dict[tuple[str, ...], int] = {}
        heading_stack: list[tuple[int, str]] = []
        for line_number, line in prose_lines:
            match = _heading_matches(path, line)
            if not match:
                continue
            heading = _normalise_heading(match.group(1))
            if not heading:
                continue
            level = _heading_level(path, line)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            scope = tuple(item[1] for item in heading_stack)
            heading_key = scope + (heading,)
            if heading_key in headings:
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "duplicate-heading",
                        f"heading repeats line {headings[heading_key]}: {match.group(1)!r}",
                        line,
                    )
                )
            else:
                headings[heading_key] = line_number
            heading_stack.append((level, heading))

        # Repeated paragraphs are usually copied boilerplate. Ignore short
        # fragments, tables, lists, and fenced code because those have other
        # legitimate repetition patterns.
        paragraphs: dict[str, int] = {}
        for match in re.finditer(r"(?s)(?:^|\n\s*\n)(.+?)(?=\n\s*\n|$)", text):
            block = match.group(1).strip()
            if (
                len(block) < 40
                or "```" in block
                or "~~~" in block
                or any(
                    line.lstrip().startswith(("|", "- ", "* ", "+ "))
                    for line in block.splitlines()
                )
            ):
                continue
            normalised = " ".join(block.split()).casefold()
            line_number = text.count("\n", 0, match.start(1)) + 1
            if normalised in paragraphs:
                findings.append(
                    _finding(
                        path,
                        line_number,
                        "duplicate-paragraph",
                        f"paragraph repeats line {paragraphs[normalised]}",
                        block.splitlines()[0],
                    )
                )
            else:
                paragraphs[normalised] = line_number

    return findings


def _pdf_finding(path: str, rule: str, message: str) -> Finding:
    return Finding(path, 1, rule, message, path)


def _read_pdf(path: Path, relative_path: str) -> tuple[str | None, list[Finding]]:
    pdfinfo = shutil.which("pdfinfo")
    pdftotext = shutil.which("pdftotext")
    if not pdfinfo or not pdftotext:
        return None, [
            _pdf_finding(
                relative_path,
                "pdf-tooling",
                "PDF checks require both pdfinfo and pdftotext from Poppler",
            )
        ]

    findings: list[Finding] = []
    try:
        metadata = subprocess.run(
            [pdfinfo, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, [
            _pdf_finding(relative_path, "pdf-metadata", f"pdfinfo failed: {error}")
        ]

    if metadata.returncode != 0:
        details = metadata.stderr.strip() or "no diagnostic output"
        findings.append(
            _pdf_finding(relative_path, "pdf-metadata", f"pdfinfo failed: {details}")
        )
    else:
        page_match = re.search(r"^Pages:\s*(\d+)\s*$", metadata.stdout, re.MULTILINE)
        if not page_match or int(page_match.group(1)) < 1:
            findings.append(
                _pdf_finding(
                    relative_path,
                    "pdf-pages",
                    "PDF metadata must report at least one page",
                )
            )

    try:
        extracted = subprocess.run(
            [pdftotext, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, findings + [
            _pdf_finding(relative_path, "pdf-text", f"pdftotext failed: {error}")
        ]

    if extracted.returncode != 0:
        details = extracted.stderr.strip() or "no diagnostic output"
        findings.append(
            _pdf_finding(relative_path, "pdf-text", f"pdftotext failed: {details}")
        )
        return None, findings
    if not extracted.stdout.strip():
        findings.append(
            _pdf_finding(
                relative_path,
                "pdf-text",
                "PDF text extraction is empty; check the LaTeX build or OCR boundary",
            )
        )
        return None, findings

    return extracted.stdout, findings


def check_repository(root: Path) -> tuple[list[Path], list[Finding]]:
    """Check every supported project document below ``root``."""

    root = root.resolve()
    files = list(iter_document_files(root))
    findings: list[Finding] = []
    for path in files:
        relative_path = _relative_path(path, root)
        if path.suffix.lower() == ".pdf":
            text, pdf_findings = _read_pdf(path, relative_path)
            findings.extend(pdf_findings)
            if text is not None:
                findings.extend(check_text(relative_path, text, is_pdf=True))
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            findings.append(
                _finding(
                    relative_path,
                    1,
                    "readability",
                    f"document cannot be read as UTF-8: {error}",
                    relative_path,
                )
            )
            continue
        findings.extend(check_text(relative_path, text))

    return files, sorted(
        findings, key=lambda finding: (finding.path, finding.line, finding.rule)
    )


def _summary(files: list[Path], findings: list[Finding]) -> str:
    counts = Counter(finding.rule for finding in findings)
    details = ", ".join(f"{rule}={counts[rule]}" for rule in sorted(counts))
    return f"Documentation check failed: {len(findings)} finding(s) in {len(files)} file(s) ({details})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root to scan (default: directory containing scripts/)",
    )
    args = parser.parse_args(argv)
    files, findings = check_repository(args.root)
    if findings:
        print(_summary(files, findings))
        for finding in findings:
            print(finding.format())
        return 1
    print(f"Documentation check passed: {len(files)} file(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
