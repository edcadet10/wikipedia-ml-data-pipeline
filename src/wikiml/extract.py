"""Latest-revision article extraction from independently compressed XML streams."""

from __future__ import annotations

import bz2
import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import suppress
from typing import cast

import mwparserfromhell
from mwparserfromhell.nodes import Heading

from wikiml.errors import FormatError
from wikiml.models import Document, DroppedPage, DropReason, ExtractionBatch

_HORIZONTAL_SPACE = re.compile(r"[\t\f\v ]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_MEDIAWIKI_CLOSE = re.compile(rb"\s*</mediawiki>\s*\Z")
_SUBSTANTIVE_WORD = re.compile(r"[^\W\d_]+", flags=re.UNICODE)
_FALLBACK_HEADING = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$")
_RESIDUAL_REF_BLOCK = re.compile(
    r"<\s*ref(?:\b|(?=name\s*=))[^>]{0,2048}>.*?</\s*ref\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_RESIDUAL_REF_SELF_CLOSING = re.compile(
    r"<\s*ref(?:\b|(?=name\s*=))(?:\s*|[^>]{0,2048}?[\"'\s])/\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_RESIDUAL_REF_TAG = re.compile(
    r"</?\s*ref(?:\b|(?=name\s*=))[^>]{0,2048}/?\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_FORMATTING_APOSTROPHES = re.compile(r"'{2,}")
_HTML_COMMENT_MARKER = re.compile(r"<!--|--!?>")
_MAGIC_WORD = re.compile(r"__[A-Z][A-Z_]+__")
_RESIDUAL_EXTERNAL_LINK = re.compile(r"\[(?:https?:)?//", flags=re.IGNORECASE)
_RESIDUAL_NONPROSE_TAG = re.compile(
    r"<\s*/?\s*(?:"
    r"ref(?:\b|(?=name\s*=))|references\b|gallery\b|imagemap\b|table\b"
    r")",
    flags=re.IGNORECASE,
)
_TABLE_ATTRIBUTE_LINE = re.compile(
    r"^\s*[|!]\s*(?:colspan|rowspan|style|class|bgcolor|align|scope)\s*=",
    flags=re.IGNORECASE,
)
_RESIDUAL_TABLE_ATTRIBUTE = re.compile(
    r"(?:^|\s)[|!]\s*(?:colspan|rowspan|style|class|bgcolor|align|scope)\s*=",
    flags=re.IGNORECASE | re.MULTILINE,
)
_RESIDUAL_TABLE_DELIMITER = re.compile(r"(?m)^\s*(?:\{\||\|\})")
_RESIDUAL_TEMPLATE = re.compile(r"\{\{|\}\}")
_RESIDUAL_TRANSCLUSION_TAG = re.compile(
    r"<\s*/?\s*(?:includeonly|noinclude|onlyinclude|nowiki)\b",
    flags=re.IGNORECASE,
)
_RESIDUAL_WIKILINK = re.compile(r"\[\[|\]\]")
_NONPROSE_LINK_PREFIXES = {
    "category",
    "draft",
    "file",
    "help",
    "image",
    "media",
    "module",
    "portal",
    "template",
    "wikipedia",
}
_NONPROSE_TAGS = {"gallery", "imagemap", "ref", "references", "table"}
_BOILERPLATE_SECTIONS = {
    "bibliography",
    "external links",
    "further reading",
    "notes",
    "other websites",
    "references",
    "related pages",
    "see also",
    "sources",
}

_RESIDUAL_MARKUP_PATTERNS = (
    _RESIDUAL_TEMPLATE,
    _RESIDUAL_WIKILINK,
    _RESIDUAL_EXTERNAL_LINK,
    _RESIDUAL_NONPROSE_TAG,
    _RESIDUAL_TRANSCLUSION_TAG,
    _RESIDUAL_TABLE_DELIMITER,
    _RESIDUAL_TABLE_ATTRIBUTE,
    _FORMATTING_APOSTROPHES,
    _HTML_COMMENT_MARKER,
    _MAGIC_WORD,
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _descendant_text(element: ET.Element, name: str) -> str:
    match = next((item for item in element.iter() if _local_name(item.tag) == name), None)
    return "" if match is None or match.text is None else match.text


def _remove_nonprose_nodes(code: mwparserfromhell.wikicode.Wikicode) -> None:
    for link in list(code.filter_wikilinks(recursive=True)):
        prefix, separator, _rest = str(link.title).strip().partition(":")
        if separator and prefix.casefold() in _NONPROSE_LINK_PREFIXES:
            with suppress(ValueError):
                code.remove(link, recursive=True)
    for tag in list(code.filter_tags(recursive=True)):
        if str(tag.tag).strip().casefold() in _NONPROSE_TAGS:
            with suppress(ValueError):
                code.remove(tag, recursive=True)


def _has_residual_markup(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _RESIDUAL_MARKUP_PATTERNS)


def _normalize_text(wikitext: str) -> str:
    wikitext = _RESIDUAL_REF_SELF_CLOSING.sub("", wikitext)
    wikitext = _RESIDUAL_REF_BLOCK.sub("", wikitext)
    wikitext = _RESIDUAL_REF_TAG.sub("", wikitext)
    code = mwparserfromhell.parse(wikitext)
    _remove_nonprose_nodes(code)

    retained_nodes: list[object] = []
    skipped_heading_level: int | None = None
    for node in code.nodes:
        if isinstance(node, Heading):
            heading = str(node.title.strip_code(normalize=True, collapse=True)).strip().casefold()
            heading = heading.rstrip(":")
            if skipped_heading_level is not None:
                if node.level > skipped_heading_level:
                    continue
                skipped_heading_level = None
            if heading in _BOILERPLATE_SECTIONS:
                skipped_heading_level = node.level
                continue
        if skipped_heading_level is None:
            retained_nodes.append(node)
    code.nodes = retained_nodes

    stripped = code.strip_code(normalize=True, collapse=True)
    # Removing an outer construct can expose balanced markup that the first parse
    # treated as literal parameter text. Reparse once, then reject anything still
    # structurally ambiguous instead of guessing where malformed markup ends.
    reparsed = mwparserfromhell.parse(stripped)
    _remove_nonprose_nodes(reparsed)
    stripped = reparsed.strip_code(normalize=True, collapse=True)
    stripped = _RESIDUAL_REF_BLOCK.sub("", stripped)
    stripped = _RESIDUAL_REF_TAG.sub("", stripped)
    normalized = unicodedata.normalize("NFC", stripped.replace("\r\n", "\n").replace("\r", "\n"))
    lines: list[str] = []
    fallback_skipped_level: int | None = None
    for raw_line in normalized.split("\n"):
        line = _HORIZONTAL_SPACE.sub(" ", raw_line).strip()
        line = _MAGIC_WORD.sub("", line)
        line = _FORMATTING_APOSTROPHES.sub("", line)
        line = _HTML_COMMENT_MARKER.sub("", line).strip()
        fallback_heading = _FALLBACK_HEADING.fullmatch(line)
        if fallback_heading is not None:
            level = len(fallback_heading[1])
            if fallback_skipped_level is not None:
                if level > fallback_skipped_level:
                    continue
                fallback_skipped_level = None
            heading_text = fallback_heading[2].strip()
            if heading_text.casefold().rstrip(":") in _BOILERPLATE_SECTIONS:
                fallback_skipped_level = level
                continue
            line = heading_text
        elif fallback_skipped_level is not None:
            continue
        if line.startswith(("|-", "|}", "{|")):
            continue
        if _TABLE_ATTRIBUTE_LINE.match(line):
            continue
        if line.startswith("|") and "||" in line:
            continue
        if line.startswith("!") and "!!" in line:
            continue
        lines.append(line)
    return _EXCESS_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _article_url(wiki: str, page_id: int) -> str:
    if not wiki.endswith("wiki") or len(wiki) <= 4:
        raise ValueError("wiki must look like 'enwiki' or 'simplewiki'")
    language = wiki[:-4]
    return f"https://{language}.wikipedia.org/?curid={page_id}"


def extract_documents(
    segment_bz2: bytes,
    *,
    wiki: str,
    expected_page_ids: tuple[int, ...] | None = None,
) -> ExtractionBatch:
    """Extract namespace-zero, non-redirect pages and retain every exclusion reason."""

    try:
        xml_fragment = bz2.decompress(segment_bz2)
    except OSError as exc:
        raise FormatError("stream is not valid bzip2 data") from exc

    # The final independently compressed stream also owns the document-level
    # closing tag. Each stream is parsed beneath our synthetic root, so remove
    # only that anchored framing tag; page text occurrences are XML-escaped.
    xml_fragment = _MEDIAWIKI_CLOSE.sub(b"", xml_fragment)

    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("end",))
    try:
        parser.feed(b"<wikiml-root>")
        parser.feed(xml_fragment)
        parser.feed(b"</wikiml-root>")
    except ET.ParseError as exc:
        raise FormatError("stream does not contain valid XML page fragments") from exc

    documents: list[Document] = []
    dropped: list[DroppedPage] = []
    pages_seen = 0
    try:
        events = cast(Iterator[tuple[str, ET.Element]], parser.read_events())
        for _event, page in events:
            if _local_name(page.tag) != "page":
                continue
            pages_seen += 1
            if expected_page_ids is not None and pages_seen > len(expected_page_ids):
                raise FormatError("stream contains more pages than its source index entries")
            expected_page_id = (
                None if expected_page_ids is None else expected_page_ids[pages_seen - 1]
            )
            title_element = _direct_child(page, "title")
            namespace_element = _direct_child(page, "ns")
            page_id_element = _direct_child(page, "id")
            title = (
                "" if title_element is None or title_element.text is None else title_element.text
            )
            try:
                page_id = int(page_id_element.text or "") if page_id_element is not None else None
            except ValueError:
                page_id = None
            try:
                namespace = (
                    int(namespace_element.text or "") if namespace_element is not None else None
                )
            except ValueError:
                namespace = None

            if expected_page_id is not None and page_id is not None and page_id != expected_page_id:
                raise FormatError("stream page order does not match its source index")

            if page_id is None or namespace is None or not title:
                dropped.append(
                    DroppedPage(
                        expected_page_id if page_id is None else page_id,
                        title,
                        DropReason.INVALID_PAGE,
                    )
                )
                page.clear()
                continue
            if namespace != 0:
                dropped.append(DroppedPage(page_id, title, DropReason.NON_ARTICLE_NAMESPACE))
                page.clear()
                continue
            if _direct_child(page, "redirect") is not None:
                dropped.append(DroppedPage(page_id, title, DropReason.REDIRECT))
                page.clear()
                continue

            revision = _direct_child(page, "revision")
            if revision is None:
                dropped.append(DroppedPage(page_id, title, DropReason.INVALID_PAGE))
                page.clear()
                continue
            revision_id_raw = _descendant_text(revision, "id")
            timestamp = _descendant_text(revision, "timestamp")
            wikitext = _descendant_text(revision, "text")
            try:
                revision_id = int(revision_id_raw)
            except ValueError:
                dropped.append(DroppedPage(page_id, title, DropReason.INVALID_PAGE))
                page.clear()
                continue
            if wikitext.lstrip().casefold().startswith("#redirect"):
                dropped.append(DroppedPage(page_id, title, DropReason.REDIRECT))
                page.clear()
                continue

            text = _normalize_text(wikitext)
            if not text:
                dropped.append(DroppedPage(page_id, title, DropReason.EMPTY_TEXT))
                page.clear()
                continue
            if _has_residual_markup(text):
                dropped.append(DroppedPage(page_id, title, DropReason.MARKUP_RESIDUE))
                page.clear()
                continue
            if len(_SUBSTANTIVE_WORD.findall(text)) < 3:
                dropped.append(DroppedPage(page_id, title, DropReason.INSUFFICIENT_TEXT))
                page.clear()
                continue
            documents.append(
                Document(
                    wiki=wiki,
                    page_id=page_id,
                    revision_id=revision_id,
                    revision_timestamp=timestamp,
                    title=title,
                    url=_article_url(wiki, page_id),
                    text=text,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                )
            )
            page.clear()
    except ET.ParseError as exc:
        raise FormatError("stream ended before its XML pages were complete") from exc

    if pages_seen == 0:
        raise FormatError("stream contained no page elements")
    if expected_page_ids is not None and pages_seen != len(expected_page_ids):
        raise FormatError("stream contains fewer pages than its source index entries")
    return ExtractionBatch(
        pages_seen=pages_seen,
        documents=tuple(sorted(documents, key=lambda item: item.page_id)),
        dropped=tuple(
            sorted(dropped, key=lambda item: -1 if item.page_id is None else item.page_id)
        ),
    )
