"""Shared pytest fixtures. Tests never touch the network or a real database."""

import fitz
import pytest

PAGE_ONE_TEXT = (
    "Revenue from contracts with customers 21 81,415.38 72,253.01\n"
    "(All amounts in Indian Rupees in million, unless otherwise stated)\n"
    "Retail headline inflation softened from 5.4 per cent in FY24 to 4.9 per cent."
)
PAGE_ONE_LABEL = "212"


def _image_only_pixmap() -> fitz.Pixmap:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 80), False)
    pixmap.clear_with(200)
    return pixmap


@pytest.fixture(scope="session")
def sample_pdf_bytes() -> bytes:
    """Three pages: prose with a printed page number, a truly blank page, an image-only page."""
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    y = 72
    for line in PAGE_ONE_TEXT.split("\n"):
        page.insert_text((72, y), line, fontsize=10)
        y += 16
    page.insert_text((290, 820), PAGE_ONE_LABEL, fontsize=9)
    document.new_page(width=595, height=842)
    image_page = document.new_page(width=595, height=842)
    image_page.insert_image(fitz.Rect(72, 72, 372, 272), pixmap=_image_only_pixmap())
    data: bytes = document.tobytes()
    document.close()
    return data
