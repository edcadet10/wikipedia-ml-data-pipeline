"""Strict Wikimedia source acquisition and multistream index handling."""

from __future__ import annotations

import bz2
import re
from dataclasses import dataclass

import httpx

from wikiml.errors import FormatError, SourceError
from wikiml.models import StreamRange

DEFAULT_USER_AGENT = (
    "wikipedia-ml-data-pipeline/0.1 (+https://github.com/edcadet10/wikipedia-ml-data-pipeline)"
)
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


@dataclass(frozen=True, slots=True)
class DownloadedBytes:
    """Response bytes plus source identity headers."""

    body: bytes
    etag: str | None
    last_modified: str | None


def parse_multistream_index(index_bz2: bytes, *, dump_size: int) -> tuple[StreamRange, ...]:
    """Convert Wikimedia's compressed offset index to inclusive stream ranges."""

    if dump_size <= 0:
        raise ValueError("dump_size must be positive")
    try:
        text = bz2.decompress(index_bz2).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FormatError("multistream index is not valid UTF-8 bzip2 data") from exc

    unique: list[tuple[int, int]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        parts = line.split(":", 2)
        if len(parts) != 3:
            raise FormatError(f"invalid multistream index line {line_number}")
        try:
            offset, page_id = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise FormatError(f"non-integer index value on line {line_number}") from exc
        if offset < 0 or page_id < 0:
            raise FormatError(f"negative index value on line {line_number}")
        if not unique or unique[-1][0] != offset:
            if unique and offset < unique[-1][0]:
                raise FormatError("multistream offsets are not monotonically increasing")
            unique.append((offset, page_id))

    if not unique:
        raise FormatError("multistream index contains no page offsets")
    if unique[-1][0] >= dump_size:
        raise FormatError("multistream offset falls outside the dump")

    ranges = []
    for ordinal, (start, page_id) in enumerate(unique):
        end = unique[ordinal + 1][0] - 1 if ordinal + 1 < len(unique) else dump_size - 1
        if end < start:
            raise FormatError("multistream range has a negative length")
        ranges.append(StreamRange(ordinal, start, end, page_id))
    return tuple(ranges)


class WikimediaClient:
    """HTTP client that rejects ambiguous or unexpectedly large responses."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("user_agent cannot be empty")
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> WikimediaClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Release pooled network connections."""

        self._client.close()

    def content_length(self, url: str) -> int:
        """Read and validate the dump's declared byte length."""

        try:
            response = self._client.head(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"failed to inspect {url}: {exc}") from exc
        raw_length = response.headers.get("Content-Length")
        if raw_length is None:
            raise SourceError(f"source did not declare Content-Length: {url}")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise SourceError(f"source declared an invalid Content-Length: {url}") from exc
        if length <= 0:
            raise SourceError(f"source declared an empty artifact: {url}")
        return length

    def download(self, url: str, *, max_bytes: int) -> DownloadedBytes:
        """Download one bounded artifact and fail before retaining an oversized body."""

        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        try:
            with self._client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > max_bytes:
                    raise SourceError(f"source exceeds {max_bytes} byte limit: {url}")
                chunks: list[bytes] = []
                observed = 0
                for chunk in response.iter_bytes():
                    observed += len(chunk)
                    if observed > max_bytes:
                        raise SourceError(f"source exceeds {max_bytes} byte limit: {url}")
                    chunks.append(chunk)
                return DownloadedBytes(
                    body=b"".join(chunks),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except SourceError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"failed to download {url}: {exc}") from exc

    def download_range(
        self, url: str, stream_range: StreamRange, *, max_bytes: int
    ) -> DownloadedBytes:
        """Fetch an exact HTTP byte range; a full-body fallback is rejected."""

        if stream_range.length > max_bytes:
            raise SourceError(
                f"requested stream is {stream_range.length} bytes; limit is {max_bytes}"
            )
        headers = {"Range": f"bytes={stream_range.start}-{stream_range.end}"}
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code != httpx.codes.PARTIAL_CONTENT:
                    raise SourceError(
                        f"source ignored exact range request (HTTP {response.status_code}): {url}"
                    )
                match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if match is None:
                    raise SourceError(f"source returned an invalid Content-Range: {url}")
                observed_start, observed_end = int(match[1]), int(match[2])
                if (observed_start, observed_end) != (stream_range.start, stream_range.end):
                    raise SourceError(f"source returned a different byte range: {url}")

                chunks: list[bytes] = []
                observed_bytes = 0
                for chunk in response.iter_bytes():
                    observed_bytes += len(chunk)
                    if observed_bytes > stream_range.length:
                        raise SourceError(f"source exceeded requested byte range: {url}")
                    chunks.append(chunk)
                if observed_bytes != stream_range.length:
                    raise SourceError(f"source returned a truncated byte range: {url}")
                return DownloadedBytes(
                    body=b"".join(chunks),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except SourceError:
            raise
        except httpx.HTTPError as exc:
            raise SourceError(f"failed to download byte range from {url}: {exc}") from exc
