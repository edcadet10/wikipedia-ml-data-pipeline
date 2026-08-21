"""One-stream vertical slice from an indexed Wikimedia dump to validated artifacts."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wikiml.errors import SourceError, ValidationError
from wikiml.extract import extract_documents
from wikiml.manifest import build_manifest, write_manifest
from wikiml.models import SplitConfig
from wikiml.source import DEFAULT_USER_AGENT, WikimediaClient, parse_multistream_catalog
from wikiml.split import partition_documents
from wikiml.storage import write_documents, write_dropped
from wikiml.tokenize import write_token_shards
from wikiml.validation import validate_dataset

_MEBIBYTE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Explicit configuration for a bounded, auditable probe run."""

    output_dir: Path
    wiki: str = "simplewiki"
    snapshot: str = "latest"
    stream_ordinal: int = 0
    base_url: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 60.0
    max_index_bytes: int = 128 * _MEBIBYTE
    max_stream_bytes: int = 64 * _MEBIBYTE
    split: SplitConfig = field(default_factory=SplitConfig)
    tokenizer_path: Path | None = None
    eos_token_id: int | None = None
    context_length: int = 1024
    sequences_per_shard: int = 4096

    def __post_init__(self) -> None:
        if not self.wiki.endswith("wiki") or not self.wiki.removesuffix("wiki").isalnum():
            raise ValueError("wiki must look like 'enwiki' or 'simplewiki'")
        if not self.snapshot.replace("-", "").isalnum():
            raise ValueError("snapshot may contain only letters, numbers, and hyphens")
        if self.stream_ordinal < 0:
            raise ValueError("stream_ordinal cannot be negative")
        if (self.tokenizer_path is None) != (self.eos_token_id is None):
            raise ValueError("tokenizer_path and eos_token_id must be supplied together")


def _source_urls(config: ProbeConfig) -> tuple[str, str, str]:
    base = (
        config.base_url.rstrip("/") + "/"
        if config.base_url
        else f"https://dumps.wikimedia.org/{config.wiki}/{config.snapshot}/"
    )
    prefix = f"{config.wiki}-{config.snapshot}-pages-articles-multistream"
    return base, base + prefix + ".xml.bz2", base + prefix + "-index.txt.bz2"


def run_probe(config: ProbeConfig) -> dict[str, Any]:
    """Run one independently compressed stream and publish only after validation."""

    target = config.output_dir.resolve()
    if target.exists():
        raise ValidationError(f"output directory already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        base_url, dump_url, index_url = _source_urls(config)
        with WikimediaClient(
            user_agent=config.user_agent, timeout_seconds=config.timeout_seconds
        ) as client:
            dump_size = client.content_length(dump_url)
            index = client.download(index_url, max_bytes=config.max_index_bytes)
            catalog = parse_multistream_catalog(index.body, dump_size=dump_size)
            ranges = catalog.ranges
            if config.stream_ordinal >= len(ranges):
                raise SourceError(
                    f"stream ordinal {config.stream_ordinal} is outside 0..{len(ranges) - 1}"
                )
            selected = ranges[config.stream_ordinal]
            segment = client.download_range(dump_url, selected, max_bytes=config.max_stream_bytes)

        extracted = extract_documents(
            segment.body,
            wiki=config.wiki,
            expected_page_ids=catalog.page_ids_by_stream[config.stream_ordinal],
        )
        documents = partition_documents(extracted.documents, config.split)
        documents_summary, content_sha256 = write_documents(documents, staging)
        dropped_summary = write_dropped(extracted.dropped, staging)
        tokenization = None
        if config.tokenizer_path is not None and config.eos_token_id is not None:
            tokenization = write_token_shards(
                documents,
                tokenizer_path=config.tokenizer_path,
                eos_token_id=config.eos_token_id,
                output_dir=staging,
                context_length=config.context_length,
                sequences_per_shard=config.sequences_per_shard,
            )

        drop_counts = Counter(item.reason.value for item in extracted.dropped)
        source = {
            "wiki": config.wiki,
            "snapshot": config.snapshot,
            "base_url": base_url,
            "dump_url": dump_url,
            "dump_bytes": dump_size,
            "index_url": index_url,
            "index_sha256": hashlib.sha256(index.body).hexdigest(),
            "index_etag": index.etag,
            "index_last_modified": index.last_modified,
            "stream": asdict(selected),
            "segment_sha256": hashlib.sha256(segment.body).hexdigest(),
            "segment_etag": segment.etag,
            "segment_last_modified": segment.last_modified,
            "latest_is_mutable": config.snapshot == "latest",
        }
        manifest = build_manifest(
            source=source,
            pages_seen=extracted.pages_seen,
            drop_counts=dict(drop_counts),
            split_config=config.split,
            documents=documents_summary,
            documents_content_sha256=content_sha256,
            dropped=dropped_summary,
            tokenization=tokenization,
        )
        write_manifest(manifest, staging)
        report = validate_dataset(staging)
        if not report.ok:
            raise ValidationError("; ".join(report.errors))
        staging.replace(target)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
