"""Strict Wikimedia source acquisition and multistream index handling."""

from __future__ import annotations

import bz2
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from wikiml.errors import FormatError, SourceError
from wikiml.models import StreamRange
from wikiml.storage import CanonicalPageIdHasher

DEFAULT_USER_AGENT = (
    "wikipedia-ml-data-pipeline/0.2 (+https://github.com/edcadet10/wikipedia-ml-data-pipeline)"
)
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


@dataclass(frozen=True, slots=True)
class DownloadedBytes:
    """Response bytes plus source identity headers."""

    body: bytes
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """A bounded streamed download plus cryptographic identity."""

    path: Path
    bytes: int
    sha1: str
    sha256: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class MultistreamIndex:
    """Validated stream ranges and the exact ordered page identities they contain."""

    ranges: tuple[StreamRange, ...]
    page_ids_by_stream: tuple[tuple[int, ...], ...]
    page_count: int
    page_ids_sha256: str


def parse_multistream_index(index_bz2: bytes, *, dump_size: int) -> tuple[StreamRange, ...]:
    """Convert Wikimedia's compressed offset index to inclusive stream ranges."""

    return parse_multistream_catalog(index_bz2, dump_size=dump_size).ranges


def parse_multistream_catalog(index_bz2: bytes, *, dump_size: int) -> MultistreamIndex:
    """Parse ranges plus every strictly ordered page ID from a multistream index."""

    if dump_size <= 0:
        raise ValueError("dump_size must be positive")
    try:
        text = bz2.decompress(index_bz2).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FormatError("multistream index is not valid UTF-8 bzip2 data") from exc

    unique: list[tuple[int, int]] = []
    page_ids_by_stream: list[list[int]] = []
    page_id_hasher = CanonicalPageIdHasher()
    previous_page_id = -1
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
        if page_id <= previous_page_id:
            raise FormatError("multistream page IDs are not strictly increasing")
        previous_page_id = page_id
        page_id_hasher.update(page_id)
        if not unique or unique[-1][0] != offset:
            if unique and offset < unique[-1][0]:
                raise FormatError("multistream offsets are not monotonically increasing")
            unique.append((offset, page_id))
            page_ids_by_stream.append([])
        page_ids_by_stream[-1].append(page_id)

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
    page_ids = tuple(tuple(group) for group in page_ids_by_stream)
    return MultistreamIndex(
        ranges=tuple(ranges),
        page_ids_by_stream=page_ids,
        page_count=sum(len(group) for group in page_ids),
        page_ids_sha256=page_id_hasher.hexdigest(),
    )


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

    def download_to_path(
        self,
        url: str,
        path: Path,
        *,
        max_bytes: int,
        expected_bytes: int | None = None,
    ) -> DownloadedFile:
        """Stream a bounded artifact to a new file while computing SHA-1 and SHA-256."""

        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if expected_bytes is not None and expected_bytes <= 0:
            raise ValueError("expected_bytes must be positive")
        if path.exists():
            raise SourceError(f"download target already exists: {path}")

        sha1 = hashlib.sha1(usedforsecurity=False)
        sha256 = hashlib.sha256()
        observed = 0
        try:
            with self._client.stream("GET", url) as response:
                response.raise_for_status()
                declared_raw = response.headers.get("Content-Length")
                if declared_raw is not None:
                    declared = int(declared_raw)
                    if declared > max_bytes:
                        raise SourceError(f"source exceeds {max_bytes} byte limit: {url}")
                    if expected_bytes is not None and declared != expected_bytes:
                        raise SourceError(f"source Content-Length changed for {url}")
                with path.open("xb") as handle:
                    for chunk in response.iter_bytes():
                        observed += len(chunk)
                        if observed > max_bytes:
                            raise SourceError(f"source exceeds {max_bytes} byte limit: {url}")
                        if expected_bytes is not None and observed > expected_bytes:
                            raise SourceError(f"source exceeded expected byte count: {url}")
                        handle.write(chunk)
                        sha1.update(chunk)
                        sha256.update(chunk)
                if expected_bytes is not None and observed != expected_bytes:
                    raise SourceError(f"source returned a truncated artifact: {url}")
                return DownloadedFile(
                    path=path,
                    bytes=observed,
                    sha1=sha1.hexdigest(),
                    sha256=sha256.hexdigest(),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except SourceError:
            path.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            path.unlink(missing_ok=True)
            raise SourceError(f"failed to download {url} to {path}: {exc}") from exc
