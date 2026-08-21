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


def test_extract_documents_accepts_final_multistream_closing_tag(segment_bz2: bytes) -> None:
    raw = bz2.decompress(segment_bz2) + b"\n</mediawiki>"

    batch = extract_documents(bz2.compress(raw), wiki="simplewiki")

    assert batch.pages_seen == 5
    assert len(batch.documents) == 2


def test_index_identity_recovers_missing_page_id() -> None:
    segment = bz2.compress(
        b"<page><title>Broken</title><ns>0</ns>"
        b"<revision><id>9</id><timestamp>2026-08-01T00:00:00Z</timestamp>"
        b"<text>content</text></revision></page>"
    )

    result = extract_documents(segment, wiki="simplewiki", expected_page_ids=(7,))

    assert result.dropped[0].page_id == 7
    assert result.dropped[0].reason is DropReason.INVALID_PAGE


@pytest.mark.parametrize(
    ("expected_page_ids", "message"),
    [
        ((1, 2, 3, 4, 5, 6), "fewer pages"),
        ((1, 2), "more pages"),
        ((99, 2, 3, 4, 5), "page order"),
    ],
)
def test_extraction_rejects_index_position_mismatch(
    segment_bz2: bytes, expected_page_ids: tuple[int, ...], message: str
) -> None:
    with pytest.raises(FormatError, match=message):
        extract_documents(
            segment_bz2,
            wiki="simplewiki",
            expected_page_ids=expected_page_ids,
        )


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


def test_extraction_removes_nonprose_markup_and_boilerplate_sections() -> None:
    xml = b"""
    <page><title>Clean</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>
      [[File:Example.jpg|right|200px]] '''Clean''' prose links to [[Useful|useful text]].
      [[Category:Examples]]
      ==References==
      Citation text that should not become training prose.
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert len(batch.documents) == 1
    assert batch.documents[0].text == "Clean prose links to useful text."


def test_extraction_removes_unexpanded_table_fragments() -> None:
    xml = b"""
    <page><title>Table</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>
      This introduction contains useful prose for a reader.
      {{table header}}
      |-bgcolor=#fefefe
      | Cell one || Cell two || align=right | 2 km
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert batch.documents[0].text == "This introduction contains useful prose for a reader."


def test_extraction_removes_parsed_and_malformed_reference_tags() -> None:
    xml = b"""
    <page><title>References</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>
      Useful opening prose remains here.&lt;ref&gt;
      Citation content is not article prose.&lt;/ref&gt;
      More useful prose remains.&lt;ref
      name="multiline"/&gt;
      Final useful prose remains.&lt;ref
      name="block"&gt;Multiline citation content is removed too.&lt;/ref&gt;
      Closing prose remains.&lt;ref name=http://example.com/&gt;
      Unquoted URL citation content is removed.&lt;/ref&gt;
      Last prose remains.&lt;refname=Amph /&gt;
      Adjacent prose remains.&lt;references&gt;
      Rendered reference-list content is removed.&lt;/references&gt;
      Final adjacent prose remains.&lt;references /&gt;
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    text = batch.documents[0].text
    assert "Useful opening prose remains here." in text
    assert "More useful prose remains." in text
    assert "Final useful prose remains." in text
    assert "Closing prose remains." in text
    assert "Last prose remains." in text
    assert "Adjacent prose remains." in text
    assert "Final adjacent prose remains." in text
    assert "Citation content" not in text
    assert "Multiline citation" not in text
    assert "Unquoted URL citation" not in text
    assert "Rendered reference-list" not in text
    assert "<ref" not in text
    assert "name=" not in text
    assert "refname" not in text
    assert "references" not in text.casefold()


def test_extraction_drops_pages_with_ambiguous_structural_markup() -> None:
    xml = b"""
    <page><title>Broken template</title><ns>0</ns><id>1</id><revision>
      <id>11</id><timestamp>x</timestamp><text>
      {{Infobox country
      | name = Broken
      Useful article prose cannot be separated safely.
      </text></revision></page>
    <page><title>Broken file</title><ns>0</ns><id>2</id><revision>
      <id>12</id><timestamp>x</timestamp><text>
      [[File:Example.jpg|thumb|An unclosed caption hides the boundary.
      Useful article prose remains structurally ambiguous.
      </text></revision></page>
    <page><title>Broken references</title><ns>0</ns><id>3</id><revision>
      <id>13</id><timestamp>x</timestamp><text>
      Useful article prose has a dangling reference-list close.
      &lt;/references&gt;
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert not batch.documents
    assert [item.reason for item in batch.dropped] == [
        DropReason.MARKUP_RESIDUE,
        DropReason.MARKUP_RESIDUE,
        DropReason.MARKUP_RESIDUE,
    ]


def test_extraction_cleans_safe_residual_control_syntax_and_table_rows() -> None:
    xml = b"""
    <page><title>Controls</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>
      Useful ''formatted'' article prose remains here. __NOTOC__
      ! colspan="2" | Table header noise
      A stray comment close --&gt; is removed conservatively.
      Final useful article prose remains here.
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert len(batch.documents) == 1
    text = batch.documents[0].text
    assert "Useful formatted article prose remains here." in text
    assert "Final useful article prose remains here." in text
    assert "Table header noise" not in text
    assert "__NOTOC__" not in text
    assert "-->" not in text


def test_extraction_drops_inline_table_attribute_residue() -> None:
    xml = b"""
    <page><title>Inline table</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>
      Useful opening article prose remains here.
      Specifications: | style="text-align:center" | 42 kg.
      More useful article prose follows the malformed fragment.
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert not batch.documents
    assert batch.dropped[0].reason is DropReason.MARKUP_RESIDUE


def test_extraction_removes_nested_tables_and_resumes_after_boilerplate() -> None:
    xml = (
        b"<page><title>Nested</title><ns>0</ns><id>1</id><revision>"
        b"<id>2</id><timestamp>x</timestamp><text>Useful opening prose stays here.\n"
        b'{| class="wikitable"\n| {| class="wikitable"\n| nested || cells\n|}\n|}\n'
        b"==References==\nCitation noise\n==History==\nUseful history prose stays too."
        b"</text></revision></page>"
    )

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert "nested" not in batch.documents[0].text
    assert "Citation" not in batch.documents[0].text
    assert "Useful history prose stays too." in batch.documents[0].text


def test_extraction_removes_nested_boilerplate_section_at_relative_depth() -> None:
    xml = b"""
    <page><title>Nested section</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>
      ==Biography==
      Useful biographical opening prose remains here.
      ===References===
      Citation noise must be removed.
      ====Footnotes====
      Nested citation noise must also be removed.
      ===Career===
      Useful career prose resumes at the peer heading.
      </text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert "Citation noise" not in batch.documents[0].text
    assert "Nested citation noise" not in batch.documents[0].text
    assert "Useful career prose resumes" in batch.documents[0].text


def test_extraction_rejects_index_mismatch_before_malformed_namespace_drop() -> None:
    xml = b"""
    <page><title>Wrong identity</title><ns>zero</ns><id>99</id><revision>
      <id>2</id><timestamp>x</timestamp><text>Useful article prose here.</text>
    </revision></page>
    """

    with pytest.raises(FormatError, match="page order"):
        extract_documents(bz2.compress(xml), wiki="simplewiki", expected_page_ids=(1,))


def test_extraction_ledgers_non_substantive_text() -> None:
    xml = b"""
    <page><title>Tiny</title><ns>0</ns><id>1</id><revision>
      <id>2</id><timestamp>x</timestamp><text>tiny</text></revision></page>
    """

    batch = extract_documents(bz2.compress(xml), wiki="simplewiki")

    assert not batch.documents
    assert batch.dropped[0].reason is DropReason.INSUFFICIENT_TEXT


def test_extract_rejects_unknown_wiki_name() -> None:
    xml = b"""
    <page><title>Article</title><ns>0</ns><id>1</id>
      <revision><id>2</id><timestamp>x</timestamp><text>Useful article text</text></revision></page>
    """
    with pytest.raises(ValueError, match="wiki"):
        extract_documents(bz2.compress(xml), wiki="unknown")
