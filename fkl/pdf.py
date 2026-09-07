"""PDF access through PyMuPDF: page inventory, printed page labels, and page rendering.

Pages are addressed by their 0-based index inside the file. Printed page labels are kept only
for display, because excerpted filings renumber, skip and even repeat printed pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import fitz

_LABEL_RE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,7})$", re.IGNORECASE)
_MIN_CHARS_FOR_TEXT_PAGE = 20


@dataclass(frozen=True)
class PageInfo:
    index: int
    label: str | None
    text: str
    char_count: int
    is_blank: bool  # no usable text and no images or drawings: nothing to extract from


def _printed_label(page: fitz.Page, text: str) -> str | None:
    """Prefer the PDF's own page label; else a bare number or roman numeral at the page edge."""
    label: str = page.get_label()
    if label:
        return label
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[-2:] + lines[:2]:
        if _LABEL_RE.match(line):
            return line
        words = line.split()
        if len(words) <= 4 and _LABEL_RE.match(words[-1]):
            return words[-1]
    return None


def _has_visual_content(page: fitz.Page) -> bool:
    return bool(page.get_images(full=False)) or bool(page.get_drawings())


def inventory(pdf_bytes: bytes) -> list[PageInfo]:
    """Extract text, printed label and blank-ness for every page."""
    pages: list[PageInfo] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        for page in document:
            text = page.get_text("text")
            has_text = len(text.strip()) >= _MIN_CHARS_FOR_TEXT_PAGE
            is_blank = not has_text and not _has_visual_content(page)
            pages.append(
                PageInfo(
                    index=page.number,
                    label=_printed_label(page, text),
                    text=text,
                    char_count=len(text),
                    is_blank=is_blank,
                )
            )
    return pages


def render_jpeg(pdf_bytes: bytes, page_index: int, width: int = 1200, quality: int = 70) -> bytes:
    """Render one page to a JPEG about `width` pixels wide (what the vision model and UI see)."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        page = document[page_index]
        zoom = width / page.rect.width
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        data: bytes = pixmap.tobytes("jpeg", jpg_quality=quality)
        return data
