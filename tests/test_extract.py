from __future__ import annotations

import bz2

import pytest

from wikiml.errors import FormatError
from wikiml.extract import extract_documents
from wikiml.models import DropReason


def test_extract_documents_preserves_provenance_and_drop_reasons(segment_bz2: bytes) -> None:
    batch = extract_documents(segment_bz2, wiki="simplewiki")

    assert batch.pages_seen == 5
    assert [document.page_id for document in batch.documents] == [1, 5]
    assert batch.documents[0].title == "Alpha"
    assert "Alpha is useful." in batch.documents[0].text
    assert batch.documents[0].url == "https://simple.wikipedia.org/?curid=1"
    assert len(batch.documents[0].text_sha256) == 64
    assert {item.reason for item in batch.dropped} == {
        DropReason.EMPTY_TEXT,
        DropReason.NON_ARTICLE_NAMESPACE,
        DropReason.REDIRECT,
    }


@pytest.mark.parametrize("payload", [b"invalid", bz2.compress(b"<broken>")])
def test_extract_documents_rejects_invalid_stream(payload: bytes) -> None:
    with pytest.raises(FormatError):
        extract_documents(payload, wiki="simplewiki")


def test_extract_documents_requires_pages() -> None:
    with pytest.raises(FormatError, match="no page"):
        extract_documents(bz2.compress(b"  "), wiki="simplewiki")


def test_extract_documents_accounts_for_malformed_and_text_redirect_pages() -> None:
    xml = b"""
    <page><title>Missing id</title><ns>0</ns></page>
    <page><title>Bad namespace</title><ns>zero</ns><id>2</id></page>
    <page><title>No revision</title><ns>0</ns><id>3</id></page>
    <page><title>Bad revision</title><ns>0</ns><id>4</id>
      <revision><id>bad</id><timestamp>x</timestamp><text>Text</text></revision></page>
    <page><title>Text redirect</title><ns>0</ns><id>5</id><revision>
      <id>105</id><timestamp>x</timestamp><text>#redirect [[Target]]</text>
    </revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert batch.pages_seen == 5
    assert not batch.documents
    assert [item.reason for item in batch.dropped].count(DropReason.INVALID_PAGE) == 4
    assert batch.dropped[-1].reason is DropReason.REDIRECT


def test_extract_rejects_unknown_wiki_name() -> None:
    xml = b"""
    <page><title>Article</title><ns>0</ns><id>1</id>
      <revision><id>2</id><timestamp>x</timestamp><text>Text</text></revision></page>
    """
    with pytest.raises(ValueError, match="wiki"):
        extract_documents(bz2.compress(xml), wiki="unknown")
