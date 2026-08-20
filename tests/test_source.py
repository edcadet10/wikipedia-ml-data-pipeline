from __future__ import annotations

import bz2
from collections.abc import Iterator

import httpx
import pytest

from wikiml.errors import FormatError, SourceError
from wikiml.models import StreamRange
from wikiml.source import WikimediaClient, parse_multistream_index


class OversizedRangeStream(httpx.SyncByteStream):
    """Fail if a client keeps consuming after the requested range is exceeded."""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield b"12345"
        yield b"6"
        raise AssertionError("the client consumed data after detecting the oversized range")

    def close(self) -> None:
        self.closed = True


def test_parse_multistream_index_collapses_duplicate_offsets() -> None:
    raw = bz2.compress(b"10:1:Alpha\n10:2:Beta:With Colon\n100:3:Gamma\n")

    ranges = parse_multistream_index(raw, dump_size=150)

    assert ranges == (
        StreamRange(ordinal=0, start=10, end=99, first_page_id=1),
        StreamRange(ordinal=1, start=100, end=149, first_page_id=3),
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"not-bzip2",
        bz2.compress(b"broken\n"),
        bz2.compress(b"ten:1:Alpha\n"),
        bz2.compress(b"100:1:Alpha\n10:2:Beta\n"),
    ],
)
def test_parse_multistream_index_rejects_invalid_content(raw: bytes) -> None:
    with pytest.raises(FormatError):
        parse_multistream_index(raw, dump_size=150)


def test_client_enforces_content_length_and_exact_range() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "20"})
        assert request.headers["Range"] == "bytes=5-9"
        return httpx.Response(
            206,
            content=b"12345",
            headers={"Content-Range": "bytes 5-9/20", "ETag": '"source"'},
        )

    with WikimediaClient(transport=httpx.MockTransport(handler)) as client:
        assert client.content_length("https://example.test/dump") == 20
        result = client.download_range(
            "https://example.test/dump",
            StreamRange(ordinal=0, start=5, end=9, first_page_id=1),
            max_bytes=10,
        )

    assert result.body == b"12345"
    assert result.etag == '"source"'


def test_client_rejects_range_fallback() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"full body"))
    with (
        WikimediaClient(transport=transport) as client,
        pytest.raises(SourceError, match="ignored exact range"),
    ):
        client.download_range(
            "https://example.test/dump",
            StreamRange(ordinal=0, start=0, end=3, first_page_id=1),
            max_bytes=10,
        )


def test_client_rejects_oversized_download() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"12345", headers={"Content-Length": "5"})
    )
    with (
        WikimediaClient(transport=transport) as client,
        pytest.raises(SourceError, match="exceeds"),
    ):
        client.download("https://example.test/index", max_bytes=4)


def test_parse_index_rejects_empty_negative_and_out_of_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_multistream_index(bz2.compress(b"1:1:Page\n"), dump_size=0)
    with pytest.raises(FormatError, match="no page offsets"):
        parse_multistream_index(bz2.compress(b""), dump_size=10)
    with pytest.raises(FormatError, match="negative"):
        parse_multistream_index(bz2.compress(b"-1:1:Page\n"), dump_size=10)
    with pytest.raises(FormatError, match="outside"):
        parse_multistream_index(bz2.compress(b"10:1:Page\n"), dump_size=10)


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "did not declare"),
        ({"Content-Length": "bad"}, "invalid"),
        ({"Content-Length": "0"}, "empty"),
    ],
)
def test_client_rejects_invalid_head_metadata(headers: dict[str, str], message: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, headers=headers))
    with WikimediaClient(transport=transport) as client, pytest.raises(SourceError, match=message):
        client.content_length("https://example.test/dump")


def test_client_downloads_bounded_body_and_metadata() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"index",
            headers={"ETag": '"index"', "Last-Modified": "today"},
        )
    )
    with WikimediaClient(transport=transport) as client:
        result = client.download("https://example.test/index", max_bytes=10)

    assert result.body == b"index"
    assert result.last_modified == "today"


@pytest.mark.parametrize(
    ("content_range", "body", "message"),
    [
        (None, b"12345", "invalid Content-Range"),
        ("bytes 4-8/20", b"12345", "different byte range"),
        ("bytes 5-9/20", b"1234", "truncated byte range"),
    ],
)
def test_client_rejects_invalid_partial_responses(
    content_range: str | None, body: bytes, message: str
) -> None:
    headers = {} if content_range is None else {"Content-Range": content_range}
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(206, content=body, headers=headers)
    )
    with WikimediaClient(transport=transport) as client, pytest.raises(SourceError, match=message):
        client.download_range(
            "https://example.test/dump",
            StreamRange(ordinal=0, start=5, end=9, first_page_id=1),
            max_bytes=10,
        )


def test_client_rejects_range_larger_than_limit() -> None:
    with (
        WikimediaClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500))
        ) as client,
        pytest.raises(SourceError, match="limit"),
    ):
        client.download_range(
            "https://example.test/dump",
            StreamRange(ordinal=0, start=0, end=10, first_page_id=1),
            max_bytes=10,
        )


def test_client_stops_streaming_when_server_exceeds_requested_range() -> None:
    stream = OversizedRangeStream()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            206,
            headers={"Content-Range": "bytes 5-9/20"},
            stream=stream,
        )
    )

    with (
        WikimediaClient(transport=transport) as client,
        pytest.raises(SourceError, match="exceeded requested byte range"),
    ):
        client.download_range(
            "https://example.test/dump",
            StreamRange(ordinal=0, start=5, end=9, first_page_id=1),
            max_bytes=10,
        )

    assert stream.closed


def test_client_requires_descriptive_user_agent() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        WikimediaClient(user_agent="")
