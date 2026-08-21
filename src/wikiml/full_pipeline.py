"""Restartable, deterministic full-dump orchestration."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import shutil
import tempfile
import warnings
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from wikiml import __version__
from wikiml.errors import SourceError, ValidationError
from wikiml.extract import extract_documents
from wikiml.manifest import write_manifest
from wikiml.models import ArtifactSummary, SplitConfig, StreamRange
from wikiml.pipeline import _MEBIBYTE
from wikiml.sampling import (
    REVIEW_PER_STRATUM,
    REVIEW_STRATA,
    NearSample,
    near_duplicate_probe,
    new_review_heaps,
    offer_near_sample,
    offer_review_candidate,
    selected_review_candidates,
)
from wikiml.source import DEFAULT_USER_AGENT, WikimediaClient, parse_multistream_catalog
from wikiml.split import assign_split, partition_documents
from wikiml.storage import (
    ATTRIBUTION_SCHEMA,
    DOCUMENT_SCHEMA,
    PAGE_DECISION_SCHEMA,
    CanonicalDocumentHasher,
    CanonicalPageIdHasher,
    canonical_document_hash,
    sha256_file,
    write_documents,
    write_dropped,
)
from wikiml.tokenize import write_token_shards_from_parquet
from wikiml.validation import validate_dataset

_GIBIBYTE = 1024 * _MEBIBYTE
FULL_PIPELINE_CONTRACT_VERSION = 7
_SHA1_LINE = re.compile(r"^([0-9a-f]{40})  (\S+)$")
_DROP_REASONS = (
    "redirect",
    "non_article_namespace",
    "empty_text",
    "insufficient_text",
    "markup_residue",
    "invalid_page",
)

_ATTRIBUTION_FIELDS = tuple(field.name for field in ATTRIBUTION_SCHEMA)

ProgressCallback = Callable[[int, int, int], None]


@dataclass(frozen=True, slots=True)
class FullBuildConfig:
    """Configuration whose identity is frozen into a resumable full-dump build."""

    output_dir: Path
    snapshot: str
    wiki: str = "simplewiki"
    work_dir: Path | None = None
    base_url: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 120.0
    workers: int = 4
    max_index_bytes: int = 128 * _MEBIBYTE
    max_dump_bytes: int = 64 * _GIBIBYTE
    split: SplitConfig = field(default_factory=SplitConfig)
    tokenizer_path: Path | None = None
    eos_token_id: int | None = None
    context_length: int = 1024
    sequences_per_shard: int = 4096
    near_duplicate_sample_size: int = 5_000
    keep_work_dir: bool = True
    fail_after_streams: int | None = None

    def __post_init__(self) -> None:
        if not self.wiki.endswith("wiki") or not self.wiki.removesuffix("wiki").isalnum():
            raise ValueError("wiki must look like 'enwiki' or 'simplewiki'")
        if re.fullmatch(r"\d{8}", self.snapshot) is None:
            raise ValueError("full builds require a dated YYYYMMDD snapshot")
        if self.workers <= 0 or self.workers > 64:
            raise ValueError("workers must be between 1 and 64")
        if self.max_index_bytes <= 0 or self.max_dump_bytes <= 0:
            raise ValueError("download limits must be positive")
        if (self.tokenizer_path is None) != (self.eos_token_id is None):
            raise ValueError("tokenizer_path and eos_token_id must be supplied together")
        if self.context_length <= 1 or self.sequences_per_shard <= 0:
            raise ValueError("token shard dimensions must be positive")
        if self.near_duplicate_sample_size <= 0:
            raise ValueError("near_duplicate_sample_size must be positive")
        if self.fail_after_streams is not None:
            if self.fail_after_streams <= 0:
                raise ValueError("fail_after_streams must be positive")
            if self.workers != 1:
                raise ValueError("deterministic failure injection requires one worker")

    @property
    def resolved_work_dir(self) -> Path:
        """Return the persistent checkpoint directory for this output."""

        target = self.output_dir.resolve()
        if self.work_dir is not None:
            return self.work_dir.resolve()
        return target.with_name(f".{target.name}.work")


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    dump_path: Path
    index_path: Path
    ranges: tuple[StreamRange, ...]
    page_ids_by_stream: tuple[tuple[int, ...], ...]
    metadata: dict[str, Any]
    identity_sha256: str


@dataclass(frozen=True, slots=True)
class _StreamJob:
    dump_path: Path
    checkpoint_root: Path
    stream: StreamRange
    expected_page_ids: tuple[int, ...]
    wiki: str
    split: SplitConfig
    identity_sha256: str


@dataclass(slots=True)
class _ExactGroup:
    first_page_id: int
    count: int
    splits: set[str]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha1_sha256_file(path: Path) -> tuple[str, str]:
    sha1 = hashlib.sha1(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * _MEBIBYTE), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def _parse_published_sha1(raw: bytes, filename: str) -> str:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SourceError("published SHA-1 manifest is not UTF-8") from exc
    matches: list[str] = []
    for line in lines:
        match = _SHA1_LINE.fullmatch(line)
        if match is not None and match[2] == filename:
            matches.append(match[1])
    if len(matches) != 1:
        raise SourceError(f"published SHA-1 manifest does not uniquely name {filename}")
    return matches[0]


def _full_source_urls(config: FullBuildConfig) -> dict[str, str]:
    base = (
        config.base_url.rstrip("/") + "/"
        if config.base_url
        else f"https://dumps.wikimedia.org/{config.wiki}/{config.snapshot}/"
    )
    prefix = f"{config.wiki}-{config.snapshot}-pages-articles-multistream"
    return {
        "base": base,
        "dump": base + prefix + ".xml.bz2",
        "index": base + prefix + "-index.txt.bz2",
        "checksums": base + f"{config.wiki}-{config.snapshot}-sha1sums.txt",
        "status": base + "dumpstatus.json",
    }


def _prepare_source(config: FullBuildConfig, work: Path) -> _PreparedSource:
    source_dir = work / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    urls = _full_source_urls(config)
    dump_name = urls["dump"].rsplit("/", 1)[-1]
    index_name = urls["index"].rsplit("/", 1)[-1]
    with WikimediaClient(
        user_agent=config.user_agent, timeout_seconds=config.timeout_seconds
    ) as client:
        status_download = client.download(urls["status"], max_bytes=16 * _MEBIBYTE)
        try:
            status = json.loads(status_download.body)
            job_status = status["jobs"]["articlesmultistreamdump"]["status"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SourceError("dump status document has an unexpected shape") from exc
        if job_status != "done":
            raise SourceError("Wikimedia articles multistream dump is not marked done")

        checksum_download = client.download(urls["checksums"], max_bytes=16 * _MEBIBYTE)
        expected_dump_sha1 = _parse_published_sha1(checksum_download.body, dump_name)
        expected_index_sha1 = _parse_published_sha1(checksum_download.body, index_name)

        dump_bytes = client.content_length(urls["dump"])
        if dump_bytes > config.max_dump_bytes:
            raise SourceError(f"dump is {dump_bytes} bytes; limit is {config.max_dump_bytes}")
        index_download = client.download(urls["index"], max_bytes=config.max_index_bytes)
        observed_index_sha1 = hashlib.sha1(index_download.body, usedforsecurity=False).hexdigest()
        if observed_index_sha1 != expected_index_sha1:
            raise SourceError("multistream index does not match Wikimedia's published SHA-1")

        dump_path = source_dir / dump_name
        if dump_path.is_file():
            if dump_path.stat().st_size != dump_bytes:
                raise SourceError("cached dump byte count does not match the dated source")
            observed_dump_sha1, observed_dump_sha256 = _sha1_sha256_file(dump_path)
        else:
            partial = dump_path.with_suffix(dump_path.suffix + ".part")
            partial.unlink(missing_ok=True)
            downloaded = client.download_to_path(
                urls["dump"],
                partial,
                max_bytes=config.max_dump_bytes,
                expected_bytes=dump_bytes,
            )
            observed_dump_sha1 = downloaded.sha1
            observed_dump_sha256 = downloaded.sha256
            if observed_dump_sha1 != expected_dump_sha1:
                partial.unlink(missing_ok=True)
                raise SourceError("dump does not match Wikimedia's published SHA-1")
            partial.replace(dump_path)

    if observed_dump_sha1 != expected_dump_sha1:
        raise SourceError("cached dump does not match Wikimedia's published SHA-1")
    index_path = source_dir / index_name
    index_path.write_bytes(index_download.body)
    checksum_path = source_dir / f"{config.wiki}-{config.snapshot}-sha1sums.txt"
    checksum_path.write_bytes(checksum_download.body)
    index_catalog = parse_multistream_catalog(index_download.body, dump_size=dump_bytes)
    metadata: dict[str, Any] = {
        "wiki": config.wiki,
        "snapshot": config.snapshot,
        "base_url": urls["base"],
        "dump_url": urls["dump"],
        "dump_bytes": dump_bytes,
        "dump_sha1": observed_dump_sha1,
        "dump_sha256": observed_dump_sha256,
        "published_dump_sha1": expected_dump_sha1,
        "index_url": urls["index"],
        "index_bytes": len(index_download.body),
        "index_sha1": observed_index_sha1,
        "index_sha256": hashlib.sha256(index_download.body).hexdigest(),
        "published_index_sha1": expected_index_sha1,
        "checksums_url": urls["checksums"],
        "checksums_sha256": hashlib.sha256(checksum_download.body).hexdigest(),
        "dump_status_url": urls["status"],
        "dump_status": job_status,
        "stream_count": len(index_catalog.ranges),
        "indexed_page_count": index_catalog.page_count,
        "indexed_page_ids_sha256": index_catalog.page_ids_sha256,
        "latest_is_mutable": False,
    }
    identity_material = {
        "source": metadata,
        "split": asdict(config.split),
        "pipeline_version": __version__,
        "pipeline_contract_version": FULL_PIPELINE_CONTRACT_VERSION,
    }
    identity_sha256 = hashlib.sha256(
        json.dumps(identity_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state_path = work / "run-state.json"
    state = {"identity_sha256": identity_sha256, **identity_material}
    if state_path.exists():
        existing = json.loads(state_path.read_text(encoding="utf-8"))
        if existing != state:
            raise ValidationError("work directory belongs to a different build identity")
    else:
        _atomic_json(state_path, state)
    return _PreparedSource(
        dump_path,
        index_path,
        index_catalog.ranges,
        index_catalog.page_ids_by_stream,
        metadata,
        identity_sha256,
    )


def _checkpoint_dir(root: Path, ordinal: int) -> Path:
    return root / f"{ordinal:06d}"


def _page_ids_sha256(page_ids: tuple[int, ...] | list[int]) -> str:
    hasher = CanonicalPageIdHasher()
    for page_id in page_ids:
        hasher.update(page_id)
    return hasher.hexdigest()


def _validate_checkpoint(
    path: Path,
    *,
    dump_path: Path,
    stream: StreamRange,
    expected_page_ids: tuple[int, ...],
    split: SplitConfig,
    identity_sha256: str,
) -> dict[str, Any]:
    try:
        checkpoint = cast(
            dict[str, Any], json.loads((path / "checkpoint.json").read_text(encoding="utf-8"))
        )
        if checkpoint.get("schema_version") != 1:
            raise ValueError("unsupported checkpoint schema")
        if checkpoint.get("identity_sha256") != identity_sha256:
            raise ValueError("checkpoint build identity mismatch")
        if checkpoint.get("stream") != asdict(stream):
            raise ValueError("checkpoint stream range mismatch")
        expected_page_id_hasher = CanonicalPageIdHasher()
        for page_id in expected_page_ids:
            expected_page_id_hasher.update(page_id)
        expected_index = {
            "pages": len(expected_page_ids),
            "page_ids_sha256": expected_page_id_hasher.hexdigest(),
        }
        if checkpoint.get("index") != expected_index:
            raise ValueError("checkpoint index identity mismatch")
        with dump_path.open("rb") as source:
            source.seek(stream.start)
            source_segment = source.read(stream.length)
        if len(source_segment) != stream.length:
            raise ValueError("checkpoint source segment is truncated")
        if hashlib.sha256(source_segment).hexdigest() != checkpoint["segment"]["sha256"]:
            raise ValueError("checkpoint source segment hash mismatch")
        artifacts = checkpoint["artifacts"]
        documents_meta = artifacts["documents"]
        dropped_meta = artifacts["dropped_pages"]
        documents_path = path / documents_meta["path"]
        dropped_path = path / dropped_meta["path"]
        for artifact_path, metadata in (
            (documents_path, documents_meta),
            (dropped_path, dropped_meta),
        ):
            if artifact_path.stat().st_size != metadata["bytes"]:
                raise ValueError("checkpoint artifact byte count mismatch")
            if sha256_file(artifact_path) != metadata["sha256"]:
                raise ValueError("checkpoint artifact hash mismatch")
        table = pq.read_table(documents_path)
        if table.schema != DOCUMENT_SCHEMA:
            raise ValueError("checkpoint document schema mismatch")
        rows = table.to_pylist()
        if len(rows) != documents_meta["records"]:
            raise ValueError("checkpoint document count mismatch")
        if canonical_document_hash(rows) != artifacts["documents_content_sha256"]:
            raise ValueError("checkpoint canonical content hash mismatch")
        page_ids = [int(row["page_id"]) for row in rows]
        if page_ids != sorted(page_ids) or len(page_ids) != len(set(page_ids)):
            raise ValueError("checkpoint page IDs are not canonical and unique")
        if any(row["split"] != assign_split(int(row["page_id"]), split) for row in rows):
            raise ValueError("checkpoint split assignment mismatch")
        dropped_rows = [
            cast(dict[str, Any], json.loads(line))
            for line in dropped_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        dropped_count = len(dropped_rows)
        extraction = checkpoint["extraction"]
        if dropped_count != dropped_meta["records"]:
            raise ValueError("checkpoint dropped-page count mismatch")
        if len(rows) + dropped_count != extraction["pages_seen"]:
            raise ValueError("checkpoint page accounting mismatch")
        if checkpoint["segment"]["bytes"] != stream.length:
            raise ValueError("checkpoint source segment byte count mismatch")
        dropped_page_ids: list[int] = []
        for row in dropped_rows:
            raw_page_id = row.get("page_id")
            if type(raw_page_id) is not int or raw_page_id < 0:
                raise ValueError(
                    "dropped-page ledger has no parsable page ID for source-index matching"
                )
            dropped_page_ids.append(raw_page_id)
        observed_page_ids = sorted([*page_ids, *dropped_page_ids])
        if observed_page_ids != list(expected_page_ids):
            raise ValueError("checkpoint decisions do not equal the indexed page IDs")
        return checkpoint
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid checkpoint {path.name}: {exc}") from exc


def _build_checkpoint(job: _StreamJob) -> dict[str, Any]:
    target = _checkpoint_dir(job.checkpoint_root, job.stream.ordinal)
    if target.exists():
        return _validate_checkpoint(
            target,
            dump_path=job.dump_path,
            stream=job.stream,
            expected_page_ids=job.expected_page_ids,
            split=job.split,
            identity_sha256=job.identity_sha256,
        )
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=job.checkpoint_root))
    try:
        with job.dump_path.open("rb") as handle:
            handle.seek(job.stream.start)
            segment = handle.read(job.stream.length)
        if len(segment) != job.stream.length:
            raise SourceError(f"local dump range {job.stream.ordinal} is truncated")
        extracted = extract_documents(
            segment, wiki=job.wiki, expected_page_ids=job.expected_page_ids
        )
        documents = partition_documents(extracted.documents, job.split)
        documents_summary, content_sha256 = write_documents(documents, staging)
        dropped_summary = write_dropped(extracted.dropped, staging)
        checkpoint: dict[str, Any] = {
            "schema_version": 1,
            "identity_sha256": job.identity_sha256,
            "stream": asdict(job.stream),
            "index": {
                "pages": len(job.expected_page_ids),
                "page_ids_sha256": _page_ids_sha256(job.expected_page_ids),
            },
            "segment": {
                "bytes": len(segment),
                "sha256": hashlib.sha256(segment).hexdigest(),
            },
            "extraction": {
                "pages_seen": extracted.pages_seen,
                "documents_emitted": len(documents),
                "pages_dropped": len(extracted.dropped),
                "drop_counts": dict(
                    sorted(Counter(item.reason.value for item in extracted.dropped).items())
                ),
            },
            "artifacts": {
                "documents": asdict(documents_summary),
                "documents_content_sha256": content_sha256,
                "dropped_pages": asdict(dropped_summary),
            },
        }
        _atomic_json(staging / "checkpoint.json", checkpoint)
        _validate_checkpoint(
            staging,
            dump_path=job.dump_path,
            stream=job.stream,
            expected_page_ids=job.expected_page_ids,
            split=job.split,
            identity_sha256=job.identity_sha256,
        )
        staging.replace(target)
        return checkpoint
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _complete_checkpoints(
    config: FullBuildConfig,
    prepared: _PreparedSource,
    work: Path,
    progress: ProgressCallback | None,
) -> tuple[list[dict[str, Any]], int]:
    checkpoint_root = work / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    for stale in checkpoint_root.glob(".*.staging-*"):
        if stale.is_dir():
            shutil.rmtree(stale)

    complete: dict[int, dict[str, Any]] = {}
    jobs: list[_StreamJob] = []
    for stream, expected_page_ids in zip(prepared.ranges, prepared.page_ids_by_stream, strict=True):
        path = _checkpoint_dir(checkpoint_root, stream.ordinal)
        if path.exists():
            complete[stream.ordinal] = _validate_checkpoint(
                path,
                dump_path=prepared.dump_path,
                stream=stream,
                expected_page_ids=expected_page_ids,
                split=config.split,
                identity_sha256=prepared.identity_sha256,
            )
        else:
            jobs.append(
                _StreamJob(
                    prepared.dump_path,
                    checkpoint_root,
                    stream,
                    expected_page_ids,
                    config.wiki,
                    config.split,
                    prepared.identity_sha256,
                )
            )
    reused = len(complete)
    total = len(prepared.ranges)
    if progress is not None:
        progress(len(complete), total, reused)

    built = 0
    if config.workers == 1:
        for job in jobs:
            complete[job.stream.ordinal] = _build_checkpoint(job)
            built += 1
            if progress is not None:
                progress(len(complete), total, reused)
            if config.fail_after_streams is not None and built >= config.fail_after_streams:
                raise ValidationError(f"injected termination after {built} newly completed streams")
    else:
        with ProcessPoolExecutor(
            max_workers=config.workers, mp_context=multiprocessing.get_context("spawn")
        ) as executor:
            for job, checkpoint in zip(
                jobs, executor.map(_build_checkpoint, jobs, chunksize=1), strict=True
            ):
                complete[job.stream.ordinal] = checkpoint
                if progress is not None:
                    progress(len(complete), total, reused)

    if len(complete) != total:
        raise ValidationError("not every indexed stream has a valid checkpoint")
    return [complete[index] for index in range(total)], reused


def _artifact(path: Path, records: int) -> ArtifactSummary:
    return ArtifactSummary(path.name, sha256_file(path), path.stat().st_size, records)


def _attribution_hash_update(hasher: hashlib._Hash, row: dict[str, Any]) -> None:
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def _write_data_card(
    path: Path,
    *,
    source: dict[str, Any],
    extraction: dict[str, Any],
    audit: dict[str, Any],
) -> None:
    text = f"""# Dataset card: {source["wiki"]} {source["snapshot"]}

## Scope

This artifact derives namespace-zero, current-revision article text from the dated
Wikimedia `pages-articles-multistream` dump. It contains {extraction["documents_emitted"]}
documents and accounts for {extraction["pages_seen"]} observed pages across
{source["stream_count"]} indexed streams.

## Provenance and reproducibility

- Source: `{source["dump_url"]}`
- Published SHA-1: `{source["published_dump_sha1"]}`
- Observed SHA-256: `{source["dump_sha256"]}`
- Snapshot status: `{source["dump_status"]}`
- Logical document SHA-256: `{audit["documents_content_sha256"]}`

## Transformations

Wikitext is parsed with `mwparserfromhell`, normalized to NFC, stripped of declared
non-prose namespaces, table markup, parsed or malformed reference blocks, and
boilerplate sections, then parsed once more to remove newly exposed balanced constructs.
Pages with ambiguous structural residue are excluded as `markup_residue`; documents with
fewer than three alphabetic words are excluded as `insufficient_text`. Every exclusion
is retained with a declared reason.

## Leakage evidence

The exhaustive exact-content and finite near-duplicate probe results are stored in
`corpus-audit.json`. Near-duplicate observations apply only to the recorded sample.

## Licensing and intended use

Repository code is Apache-2.0. The extracted text is modified Wikipedia content and
remains subject to CC BY-SA 4.0, possible additional source terms, attribution,
ShareAlike, license-notice, and change-indication obligations. `attribution.parquet`
preserves page and revision identity, while this card documents that markup was removed
and text was normalized. Review the linked page history and applicable source terms;
this card is not legal advice or a legal-compliance determination.

## Known limitations

Semantic cleaning quality requires the separately recorded human review. Templates,
tables, references, language variation, and unusual markup may be imperfectly rendered.
This dataset is intended for research and pipeline evaluation, not unreviewed deployment.
"""
    path.write_text(text, encoding="utf-8")


def _merge_checkpoints(
    checkpoints: list[dict[str, Any]],
    *,
    checkpoint_root: Path,
    staging: Path,
    config: FullBuildConfig,
    source: dict[str, Any],
    source_index_path: Path,
) -> dict[str, Any]:
    documents_path = staging / "documents.parquet"
    attribution_path = staging / "attribution.parquet"
    dropped_path = staging / "dropped-pages.jsonl"
    streams_path = staging / "streams.jsonl"
    page_decisions_path = staging / "page-decisions.parquet"
    source_index_artifact_path = staging / "source-index.txt.bz2"
    documents_writer = pq.ParquetWriter(
        documents_path,
        DOCUMENT_SCHEMA,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        version="2.6",
    )
    attribution_writer = pq.ParquetWriter(
        attribution_path,
        ATTRIBUTION_SCHEMA,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        version="2.6",
    )
    decisions_writer = pq.ParquetWriter(
        page_decisions_path,
        PAGE_DECISION_SCHEMA,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        version="2.6",
    )
    content_hasher = CanonicalDocumentHasher()
    attribution_hasher = hashlib.sha256()
    exact_groups: dict[str, _ExactGroup] = {}
    near_heap: list[tuple[int, int, NearSample]] = []
    review_heaps = new_review_heaps()
    total_documents = 0
    total_dropped = 0
    total_pages = 0
    drop_counts: Counter[str] = Counter()
    previous_page_id = -1
    total_decisions = 0
    try:
        with (
            dropped_path.open("w", encoding="utf-8", newline="\n") as dropped_output,
            streams_path.open("w", encoding="utf-8", newline="\n") as streams_output,
        ):
            for checkpoint in checkpoints:
                ordinal = int(checkpoint["stream"]["ordinal"])
                checkpoint_path = _checkpoint_dir(checkpoint_root, ordinal)
                stream_record = {
                    "stream": checkpoint["stream"],
                    "index": checkpoint["index"],
                    "segment": checkpoint["segment"],
                    "extraction": checkpoint["extraction"],
                    "documents_content_sha256": checkpoint["artifacts"]["documents_content_sha256"],
                }
                streams_output.write(
                    json.dumps(stream_record, sort_keys=True, separators=(",", ":")) + "\n"
                )
                extraction = checkpoint["extraction"]
                total_pages += int(extraction["pages_seen"])
                total_documents += int(extraction["documents_emitted"])
                total_dropped += int(extraction["pages_dropped"])
                drop_counts.update(extraction["drop_counts"])

                document_path = checkpoint_path / checkpoint["artifacts"]["documents"]["path"]
                parquet = pq.ParquetFile(document_path)
                checkpoint_document_ids: list[int] = []
                for batch in parquet.iter_batches(batch_size=256):
                    rows = cast(list[dict[str, Any]], batch.to_pylist())
                    for row in rows:
                        page_id = int(row["page_id"])
                        if page_id <= previous_page_id:
                            raise ValidationError(
                                "page IDs are not strictly increasing across stream checkpoints"
                            )
                        previous_page_id = page_id
                        checkpoint_document_ids.append(page_id)
                        content_hasher.update(row)
                        attribution = {field: row[field] for field in _ATTRIBUTION_FIELDS}
                        _attribution_hash_update(attribution_hasher, attribution)
                        text_hash = str(row["text_sha256"])
                        group = exact_groups.get(text_hash)
                        if group is None:
                            exact_groups[text_hash] = _ExactGroup(page_id, 1, {str(row["split"])})
                        else:
                            group.count += 1
                            group.splits.add(str(row["split"]))
                        offer_near_sample(near_heap, row, config.near_duplicate_sample_size)
                        offer_review_candidate(review_heaps, row)
                    table = pa.Table.from_pylist(rows, schema=DOCUMENT_SCHEMA)
                    documents_writer.write_table(table)
                    attribution_rows = [
                        {field: row[field] for field in _ATTRIBUTION_FIELDS} for row in rows
                    ]
                    attribution_writer.write_table(
                        pa.Table.from_pylist(attribution_rows, schema=ATTRIBUTION_SCHEMA)
                    )

                checkpoint_dropped = (
                    checkpoint_path / checkpoint["artifacts"]["dropped_pages"]["path"]
                )
                checkpoint_dropped_rows: list[dict[str, Any]] = []
                with checkpoint_dropped.open(encoding="utf-8") as dropped_input:
                    for line in dropped_input:
                        dropped_output.write(line)
                        if line.strip():
                            checkpoint_dropped_rows.append(cast(dict[str, Any], json.loads(line)))
                decision_rows: list[dict[str, Any]] = [
                    {
                        "page_id": page_id,
                        "stream_ordinal": ordinal,
                        "decision": "emitted",
                        "reason": None,
                    }
                    for page_id in checkpoint_document_ids
                ]
                decision_rows.extend(
                    {
                        "page_id": int(row["page_id"]),
                        "stream_ordinal": ordinal,
                        "decision": "dropped",
                        "reason": str(row["reason"]),
                    }
                    for row in checkpoint_dropped_rows
                )
                decision_rows.sort(key=lambda row: int(row["page_id"]))
                decisions_writer.write_table(
                    pa.Table.from_pylist(decision_rows, schema=PAGE_DECISION_SCHEMA)
                )
                total_decisions += len(decision_rows)
    finally:
        documents_writer.close()
        attribution_writer.close()
        decisions_writer.close()

    if total_documents + total_dropped != total_pages:
        raise ValidationError("full merge did not account for every observed page")
    if total_decisions != total_pages:
        raise ValidationError("page-decision ledger does not account for every observed page")
    shutil.copyfile(source_index_path, source_index_artifact_path)
    duplicate_groups = [group for group in exact_groups.values() if group.count > 1]
    cross_split_groups = [group for group in duplicate_groups if len(group.splits) > 1]
    exact_examples = [
        {
            "first_page_id": group.first_page_id,
            "documents": group.count,
            "splits": sorted(group.splits),
        }
        for group in cross_split_groups[:20]
    ]
    review_candidates = selected_review_candidates(review_heaps)
    audit: dict[str, Any] = {
        "documents_content_sha256": content_hasher.hexdigest(),
        "page_id_split_intersection_count": 0,
        "exact_duplicates": {
            "method": "exhaustive SHA-256 equality over normalized extracted text",
            "duplicate_groups": len(duplicate_groups),
            "cross_split_groups": len(cross_split_groups),
            "cross_split_examples": exact_examples,
        },
        "near_duplicates": near_duplicate_probe(near_heap, config.near_duplicate_sample_size),
        "semantic_review": {
            "method": "lowest SHA-256 semantic-review-v1 page scores per length stratum",
            "strata": list(REVIEW_STRATA),
            "candidates_per_stratum": REVIEW_PER_STRATUM,
            "candidate_count": len(review_candidates),
            "inference_boundary": "reviewed sample only; not population accuracy",
        },
    }
    audit_path = staging / "corpus-audit.json"
    _atomic_json(audit_path, audit)

    review_path = staging / "semantic-review-candidates.jsonl"
    with review_path.open("w", encoding="utf-8", newline="\n") as handle:
        for candidate in review_candidates:
            handle.write(
                json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
    review_count = len(review_candidates)

    extraction_summary = {
        "pages_seen": total_pages,
        "documents_emitted": total_documents,
        "pages_dropped": total_dropped,
        "drop_counts": {reason: drop_counts.get(reason, 0) for reason in _DROP_REASONS},
    }
    data_card_path = staging / "DATA_CARD.md"
    _write_data_card(
        data_card_path,
        source=source,
        extraction=extraction_summary,
        audit=audit,
    )
    return {
        "extraction": extraction_summary,
        "documents": asdict(_artifact(documents_path, total_documents)),
        "documents_content_sha256": audit["documents_content_sha256"],
        "dropped_pages": asdict(_artifact(dropped_path, total_dropped)),
        "streams": asdict(_artifact(streams_path, len(checkpoints))),
        "attribution": asdict(_artifact(attribution_path, total_documents)),
        "attribution_content_sha256": attribution_hasher.hexdigest(),
        "page_decisions": asdict(_artifact(page_decisions_path, total_decisions)),
        "source_index": asdict(_artifact(source_index_artifact_path, total_decisions)),
        "corpus_audit": asdict(_artifact(audit_path, 1)),
        "semantic_review_candidates": asdict(_artifact(review_path, review_count)),
        "data_card": asdict(_artifact(data_card_path, 1)),
        "audit": audit,
    }


def run_full_build(
    config: FullBuildConfig, *, progress: ProgressCallback | None = None
) -> dict[str, Any]:
    """Build one dated dump with resumable checkpoints and atomic final publication."""

    target = config.output_dir.resolve()
    work = config.resolved_work_dir
    if target.exists():
        raise ValidationError(f"output directory already exists: {target}")
    if work == target or work.is_relative_to(target) or target.is_relative_to(work):
        raise ValidationError("work and output directories must be disjoint")
    target.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    prepared = _prepare_source(config, work)
    checkpoints, reused = _complete_checkpoints(config, prepared, work, progress)

    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        merged = _merge_checkpoints(
            checkpoints,
            checkpoint_root=work / "checkpoints",
            staging=staging,
            config=config,
            source=prepared.metadata,
            source_index_path=prepared.index_path,
        )
        tokenization = None
        training_smoke = None
        if config.tokenizer_path is not None and config.eos_token_id is not None:
            tokenization_summary, training_smoke = write_token_shards_from_parquet(
                staging / "documents.parquet",
                tokenizer_path=config.tokenizer_path,
                eos_token_id=config.eos_token_id,
                output_dir=staging,
                context_length=config.context_length,
                sequences_per_shard=config.sequences_per_shard,
            )
            tokenization = asdict(tokenization_summary)

        manifest: dict[str, Any] = {
            "schema_version": 2,
            "project": {
                "name": "wikipedia-ml-data-pipeline",
                "version": __version__,
                "scope": "complete_dated_multistream_dump",
                "pipeline_contract_version": FULL_PIPELINE_CONTRACT_VERSION,
            },
            "source": prepared.metadata,
            "execution": {
                "workers": config.workers,
                "checkpoint_identity_sha256": prepared.identity_sha256,
                "checkpoints_reused": reused,
                "checkpoints_built": len(checkpoints) - reused,
                "deterministic_merge_order": "stream ordinal, then ascending page_id",
            },
            "extraction": merged["extraction"],
            "split": {**asdict(config.split), "strategy": "page_id_sha256_basis_points"},
            "artifacts": {
                "documents": merged["documents"],
                "documents_content_sha256": merged["documents_content_sha256"],
                "dropped_pages": merged["dropped_pages"],
                "streams": merged["streams"],
                "attribution": merged["attribution"],
                "attribution_content_sha256": merged["attribution_content_sha256"],
                "page_decisions": merged["page_decisions"],
                "source_index": merged["source_index"],
                "corpus_audit": merged["corpus_audit"],
                "semantic_review_candidates": merged["semantic_review_candidates"],
                "data_card": merged["data_card"],
                "tokenization": tokenization,
                "training_smoke": training_smoke,
            },
            "claims": {
                "full_dump_validated": False,
                "semantic_extraction_validated": False,
                "human_licensing_reviewed": False,
                "tested_scope": "mechanically validated complete dated dump",
            },
        }
        write_manifest(manifest, staging)
        report = validate_dataset(staging)
        if not report.ok:
            raise ValidationError("; ".join(report.errors))
        # tempfile.mkdtemp intentionally creates mode 0700. Published datasets
        # are read-only artifacts for downstream users, so make the root
        # traversable before the atomic rename; contained files retain their
        # ordinary umask-derived read permissions.
        staging.chmod(0o755)
        staging.replace(target)
        if not config.keep_work_dir:
            try:
                shutil.rmtree(work)
            except OSError as exc:
                warnings.warn(
                    f"dataset was published but work directory cleanup failed: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
