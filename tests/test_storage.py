from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from wikiml.models import Document, DroppedPage, DropReason
from wikiml.storage import (
    DOCUMENT_SCHEMA,
    canonical_document_hash,
    document_rows,
    read_document_rows,
    write_documents,
    write_dropped,
)


def _document(page_id: int) -> Document:
    return Document(
        wiki="simplewiki",
        page_id=page_id,
        revision_id=page_id + 100,
        revision_timestamp="2026-08-20T12:00:00Z",
        title=f"Page {page_id}",
        url=f"https://simple.wikipedia.org/?curid={page_id}",
        text=f"Text {page_id}",
        text_sha256=str(page_id) * 64,
        split="train",
    )


def test_document_storage_has_declared_schema_and_order_independent_content_hash(
    tmp_path: Path,
) -> None:
    documents = (_document(1), _document(2))

    summary, content_hash = write_documents(documents, tmp_path)

    assert summary.records == 2
    assert pq.read_schema(tmp_path / summary.path) == DOCUMENT_SCHEMA
    assert read_document_rows(tmp_path / summary.path) == document_rows(documents)
    assert content_hash == canonical_document_hash(reversed(document_rows(documents)))


def test_dropped_storage_is_stable_jsonl(tmp_path: Path) -> None:
    dropped = (DroppedPage(2, "Redirect", DropReason.REDIRECT),)

    summary = write_dropped(dropped, tmp_path)

    assert summary.records == 1
    assert (tmp_path / summary.path).read_text(encoding="utf-8") == (
        '{"page_id":2,"reason":"redirect","title":"Redirect"}\n'
    )
