"""PDF inventory (text, labels, blank detection) and page rendering via PyMuPDF."""

import pymupdf

from fkl.pdf import inventory, render_jpeg
from tests.conftest import PAGE_ONE_LABEL


def test_inventory_lists_every_page_with_text_and_char_count(sample_pdf_bytes: bytes) -> None:
    pages = inventory(sample_pdf_bytes)
    assert [p.index for p in pages] == [0, 1, 2]
    assert "81,415.38" in pages[0].text
    assert pages[0].char_count == len(pages[0].text)


def test_inventory_reads_printed_page_label_from_page_text(sample_pdf_bytes: bytes) -> None:
    pages = inventory(sample_pdf_bytes)
    assert pages[0].label == PAGE_ONE_LABEL
    assert pages[1].label is None


def test_blank_page_is_flagged_but_image_only_page_is_not(sample_pdf_bytes: bytes) -> None:
    pages = inventory(sample_pdf_bytes)
    assert pages[1].is_blank is True
    assert pages[2].is_blank is False and pages[2].text.strip() == ""


def test_render_jpeg_produces_jpeg_of_requested_width(sample_pdf_bytes: bytes) -> None:
    data = render_jpeg(sample_pdf_bytes, page_index=0, width=600)
    assert data[:2] == b"\xff\xd8"
    pixmap = pymupdf.Pixmap(data)
    assert abs(pixmap.width - 600) <= 1
    assert pixmap.height > pixmap.width  # portrait page keeps its aspect ratio
