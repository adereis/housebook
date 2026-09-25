"""Thin wrappers around pdftotext / OCR.

These helpers exist so the AI agent (during import) and the test
suite both call into a single, well-tested code path. The import
SOPs explicitly tell the agent to invoke `pdftotext` via Bash; this
module is the deterministic equivalent for code that needs to
extract text without involving the agent.

Nothing here is layout-aware on purpose — layout-aware extraction is
the AI agent's job. The wrappers handle invocation, encoding, and the
"empty PDF" failure mode that used to mean a parser format-change.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class PdfToolError(Exception):
    """Raised when an underlying CLI tool is missing or fails."""


@dataclass
class PageText:
    page: int               # 1-indexed
    text: str


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise PdfToolError(
            f"`{tool}` is not installed; import needs it"
        )


def pdf_to_text(
    path: str, layout: bool = True, encoding: str = "utf-8",
) -> str:
    """Run `pdftotext` and return the entire document as text."""
    _require("pdftotext")
    args = ["pdftotext"]
    if layout:
        args.append("-layout")
    args += [path, "-"]
    try:
        result = subprocess.run(
            args, capture_output=True, check=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        raise PdfToolError(
            f"pdftotext failed on {path}: "
            f"{e.stderr.decode(encoding, 'replace')}"
        ) from e
    return result.stdout.decode(encoding, "replace")


def pdf_pages(
    path: str, layout: bool = True, encoding: str = "utf-8",
) -> list[PageText]:
    """Return text per page. Page indices are 1-based.

    Pages are split on the form-feed character that pdftotext emits
    between pages (\\f).
    """
    text = pdf_to_text(path, layout=layout, encoding=encoding)
    if "\f" not in text:
        return [PageText(page=1, text=text)]
    return [
        PageText(page=i + 1, text=chunk)
        for i, chunk in enumerate(text.split("\f"))
        if chunk.strip()
    ]


def pdf_is_empty(path: str) -> bool:
    """True if pdftotext returns no non-whitespace text.

    This is the canonical 'parser would have produced 0 transactions'
    signal. Used by import SOPs to decide whether to fall through
    to OCR.
    """
    return not pdf_to_text(path).strip()


def ocr_pdf(path: str, lang: str = "eng") -> str:
    """OCR fallback for image-only PDFs via `tesseract` + `pdftoppm`.

    Slow; only used when pdf_is_empty(path) is True. Returns the
    concatenated OCR text.
    """
    _require("pdftoppm")
    _require("tesseract")
    import os
    import tempfile

    out: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        prefix = os.path.join(td, "page")
        try:
            subprocess.run(
                ["pdftoppm", "-r", "300", "-png", path, prefix],
                check=True, capture_output=True, timeout=300,
            )
        except subprocess.CalledProcessError as e:
            raise PdfToolError(
                f"pdftoppm failed on {path}: "
                f"{e.stderr.decode('utf-8', 'replace')}"
            ) from e

        pages = sorted(
            f for f in os.listdir(td) if f.endswith(".png")
        )
        for png in pages:
            try:
                r = subprocess.run(
                    ["tesseract", os.path.join(td, png), "-",
                     "-l", lang, "--psm", "6"],
                    check=True, capture_output=True, timeout=120,
                )
            except subprocess.CalledProcessError as e:
                raise PdfToolError(
                    f"tesseract failed on {png}: "
                    f"{e.stderr.decode('utf-8', 'replace')}"
                ) from e
            out.append(r.stdout.decode("utf-8", "replace"))
    return "\n\f\n".join(out)
