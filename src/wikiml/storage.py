"""Canonical document, exclusion, and hashing utilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from wikiml.models import ArtifactSummary, Document, DroppedPage

DOCUMENT_SCHEMA = pa.schema(
    [
        ("wiki", pa.string()),
        ("page_id", pa.int64()),
        ("revision_id", pa.int64()),
        ("revision_timestamp", pa.string()),
        ("title", pa.string()),
        ("url", pa.string()),
        ("text", pa.string()),
        ("text_sha256", pa.string()),
        ("split", pa.string()),
        ("license", pa.string()),
    ],
    metadata={
        b"wikiml.schema_version": b"1",
        b"wikiml.content_notice": b"Wikipedia text is subject to its source license",
    },
)


def sha256_file(path: Path) -> str:
    """Hash a file without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_document_hash(rows: Iterable[dict[str, Any]]) -> str:
    """Hash logical document content independently of Parquet container metadata."""

    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: int(item["page_id"])):
        encoded = json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def document_rows(documents: Iterable[Document]) -> list[dict[str, Any]]:
    """Convert typed documents into the exact public data contract."""

    return [asdict(document) for document in documents]


def write_documents(
    documents: tuple[Document, ...], output_dir: Path
) -> tuple[ArtifactSummary, str]:
    """Write deterministic-order document Parquet and return file and content hashes."""

    rows = document_rows(documents)
    path = output_dir / "documents.parquet"
    table = pa.Table.from_pylist(rows, schema=DOCUMENT_SCHEMA)
    pq.write_table(
        table,
        path,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        version="2.6",
    )
    return (
        ArtifactSummary(
            path=path.name,
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            records=len(rows),
        ),
        canonical_document_hash(rows),
    )


def write_dropped(dropped: tuple[DroppedPage, ...], output_dir: Path) -> ArtifactSummary:
    """Write exclusions as stable JSON Lines so every input page is accounted for."""

    path = output_dir / "dropped-pages.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in dropped:
            row = {"page_id": item.page_id, "reason": item.reason.value, "title": item.title}
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            )
    return ArtifactSummary(
        path=path.name,
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        records=len(dropped),
    )


def read_document_rows(path: Path) -> list[dict[str, Any]]:
    """Read the logical rows used by integrity validation."""

    return cast(list[dict[str, Any]], pq.read_table(path).to_pylist())
