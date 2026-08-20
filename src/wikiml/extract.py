"""Latest-revision article extraction from independently compressed XML streams."""

from __future__ import annotations

import bz2
import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import cast

import mwparserfromhell

from wikiml.errors import FormatError
from wikiml.models import Document, DroppedPage, DropReason, ExtractionBatch

_HORIZONTAL_SPACE = re.compile(r"[\t\f\v ]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _descendant_text(element: ET.Element, name: str) -> str:
    match = next((item for item in element.iter() if _local_name(item.tag) == name), None)
    return "" if match is None or match.text is None else match.text


def _normalize_text(wikitext: str) -> str:
    stripped = mwparserfromhell.parse(wikitext).strip_code(normalize=True, collapse=True)
    normalized = unicodedata.normalize("NFC", stripped.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in normalized.split("\n")]
    return _EXCESS_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _article_url(wiki: str, page_id: int) -> str:
    if not wiki.endswith("wiki") or len(wiki) <= 4:
        raise ValueError("wiki must look like 'enwiki' or 'simplewiki'")
    language = wiki[:-4]
    return f"https://{language}.wikipedia.org/?curid={page_id}"


def extract_documents(segment_bz2: bytes, *, wiki: str) -> ExtractionBatch:
    """Extract namespace-zero, non-redirect pages and retain every exclusion reason."""

    try:
        xml_fragment = bz2.decompress(segment_bz2)
    except OSError as exc:
        raise FormatError("stream is not valid bzip2 data") from exc

    parser = ET.XMLPullParser(events=("end",))
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
            title_element = _direct_child(page, "title")
            namespace_element = _direct_child(page, "ns")
            page_id_element = _direct_child(page, "id")
            title = (
                "" if title_element is None or title_element.text is None else title_element.text
            )
            try:
                page_id = int(page_id_element.text or "") if page_id_element is not None else None
                namespace = (
                    int(namespace_element.text or "") if namespace_element is not None else None
                )
            except ValueError:
                page_id = None
                namespace = None

            if page_id is None or namespace is None or not title:
                dropped.append(DroppedPage(page_id, title, DropReason.INVALID_PAGE))
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
    return ExtractionBatch(
        pages_seen=pages_seen,
        documents=tuple(sorted(documents, key=lambda item: item.page_id)),
        dropped=tuple(
            sorted(dropped, key=lambda item: -1 if item.page_id is None else item.page_id)
        ),
    )
