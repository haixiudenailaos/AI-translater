"""Regression tests for single-pass EPUB item indexing."""

from __future__ import annotations

from ebooklib import epub

from src.core.epub_processor import EPUBProcessor


def test_import_indexes_book_items_once(tmp_app_paths, make_epub, monkeypatch):
    """Manifest, CSS and image extraction must reuse one item enumeration."""
    epub_path = make_epub(
        chapters=[("chapter", ["text"])],
        images=[("Images/page.png", b"\x89PNG\r\n\x1a\nimage")],
    )
    read_epub = epub.read_epub
    get_items_calls = 0

    def counted_read_epub(*args, **kwargs):
        nonlocal get_items_calls
        book = read_epub(*args, **kwargs)
        original_get_items = book.get_items

        def counted_get_items():
            nonlocal get_items_calls
            get_items_calls += 1
            return original_get_items()

        monkeypatch.setattr(book, "get_items", counted_get_items)
        return book

    monkeypatch.setattr(epub, "read_epub", counted_read_epub)

    EPUBProcessor(app_paths=tmp_app_paths).import_epub(str(epub_path), extract_images=True)

    assert get_items_calls == 1
