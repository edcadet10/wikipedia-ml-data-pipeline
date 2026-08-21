"""Streaming validation for schema-v2 complete-dump artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow.parquet as pq

from wikiml.errors import WikiMLError
from wikiml.models import SplitConfig, ValidationReport
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
from wikiml.source import parse_multistream_catalog
from wikiml.split import assign_split
from wikiml.storage import (
    ATTRIBUTION_SCHEMA,
    DOCUMENT_SCHEMA,
    PAGE_DECISION_SCHEMA,
    CanonicalDocumentHasher,
    CanonicalPageIdHasher,
    sha256_file,
)

_DTYPES: dict[str, str] = {"uint16-le": "<u2", "uint32-le": "<u4"}
_PIPELINE_CONTRACT_VERSION = 6
_DATED_SNAPSHOT = re.compile(r"^\d{8}$")
_RESIDUAL_MARKUP_PATTERNS = (
    re.compile(r"\{\{|\}\}"),
    re.compile(r"\[\[|\]\]"),
    re.compile(r"\[(?:https?:)?//", flags=re.IGNORECASE),
    re.compile(
        r"<\s*/?\s*(?:"
        r"ref(?:\b|(?=name\s*=))|references\b|gallery\b|imagemap\b|table\b"
        r")",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"<\s*/?\s*(?:includeonly|noinclude|onlyinclude|nowiki)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(r"(?m)^\s*(?:\{\||\|\})"),
    re.compile(r"(?im)(?:^|\s)[|!]\s*(?:colspan|rowspan|style|class|bgcolor|align|scope)\s*="),
    re.compile(r"'{2,}"),
    re.compile(r"<!--|-->"),
    re.compile(r"__[A-Z][A-Z_]+__"),
)
_DROP_REASONS = {
    "redirect",
    "non_article_namespace",
    "empty_text",
    "insufficient_text",
    "markup_residue",
    "invalid_page",
}
_ATTRIBUTION_FIELDS = tuple(field.name for field in ATTRIBUTION_SCHEMA)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _artifact_path(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ValueError("artifact path must be a non-empty relative path")
    resolved_root = root.resolve()
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("artifact path escapes the dataset directory")
    return resolved


def _attribution_hash_update(hasher: Any, row: dict[str, Any]) -> None:
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def validate_full_dataset(root: Path, manifest: Mapping[str, Any]) -> ValidationReport:
    """Validate a complete dump without materializing the corpus in memory."""

    checks: list[str] = []
    errors: list[str] = []
    try:
        project = _mapping(manifest.get("project"), "project")
        source = _mapping(manifest.get("source"), "source")
        extraction = _mapping(manifest.get("extraction"), "extraction")
        artifacts = _mapping(manifest.get("artifacts"), "artifacts")
        if project.get("scope") != "complete_dated_multistream_dump":
            errors.append("schema-v2 project scope mismatch")
        if project.get("pipeline_contract_version") != _PIPELINE_CONTRACT_VERSION:
            errors.append("unsupported full-pipeline contract version")
        snapshot = source.get("snapshot")
        if not isinstance(snapshot, str) or _DATED_SNAPSHOT.fullmatch(snapshot) is None:
            errors.append("full dump source is not a dated snapshot")
        if source.get("dump_status") != "done":
            errors.append("Wikimedia dump was not marked done")
        if source.get("dump_sha1") != source.get("published_dump_sha1"):
            errors.append("dump SHA-1 does not match the published checksum")
        if source.get("index_sha1") != source.get("published_index_sha1"):
            errors.append("index SHA-1 does not match the published checksum")
        checks.append("dated source and published checksum identity")

        artifact_labels = (
            "documents",
            "dropped_pages",
            "streams",
            "attribution",
            "page_decisions",
            "source_index",
            "corpus_audit",
            "semantic_review_candidates",
            "data_card",
        )
        artifact_meta: dict[str, Mapping[str, Any]] = {}
        artifact_paths: dict[str, Path] = {}
        for label in artifact_labels:
            metadata = _mapping(artifacts.get(label), f"{label} artifact")
            path = _artifact_path(root, metadata.get("path"))
            artifact_meta[label] = metadata
            artifact_paths[label] = path
            if not path.is_file():
                errors.append(f"missing {label} artifact")
                continue
            if path.stat().st_size != metadata.get("bytes"):
                errors.append(f"{label} byte count mismatch")
            if sha256_file(path) != metadata.get("sha256"):
                errors.append(f"{label} SHA-256 mismatch")
        if errors:
            return ValidationReport(tuple(checks), tuple(errors))
        checks.append("all declared non-token artifact hashes")

        source_index_bytes = artifact_paths["source_index"].read_bytes()
        source_index_sha1 = hashlib.sha1(source_index_bytes, usedforsecurity=False).hexdigest()
        if source_index_sha1 != source.get("index_sha1"):
            errors.append("bundled source index SHA-1 does not match source identity")
        if hashlib.sha256(source_index_bytes).hexdigest() != source.get("index_sha256"):
            errors.append("bundled source index SHA-256 does not match source identity")
        index_catalog = parse_multistream_catalog(
            source_index_bytes, dump_size=int(source["dump_bytes"])
        )
        if index_catalog.page_count != source.get("indexed_page_count"):
            errors.append("indexed page count does not match the source index")
        if index_catalog.page_ids_sha256 != source.get("indexed_page_ids_sha256"):
            errors.append("indexed page-ID hash does not match the source index")
        if artifact_meta["source_index"].get("records") != index_catalog.page_count:
            errors.append("source-index artifact record count mismatch")
        checks.append("bundled published source index identity")

        audit = _mapping(
            json.loads(artifact_paths["corpus_audit"].read_text(encoding="utf-8")),
            "corpus audit",
        )
        declared_near = _mapping(audit.get("near_duplicates"), "near duplicate audit")
        near_sample_size = int(declared_near["requested_sample_size"])
        if near_sample_size <= 0:
            raise ValueError("near-duplicate sample size must be positive")
        near_heap: list[tuple[int, int, NearSample]] = []
        review_heaps = new_review_heaps()

        streams_path = artifact_paths["streams"]
        stream_count = 0
        stream_pages = 0
        stream_documents = 0
        stream_dropped = 0
        stream_drop_counts: Counter[str] = Counter()
        previous_end: int | None = None
        with streams_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = _mapping(json.loads(line), "stream record")
                stream = _mapping(record.get("stream"), "stream")
                stream_index = _mapping(record.get("index"), "stream index")
                segment = _mapping(record.get("segment"), "segment")
                stream_extraction = _mapping(record.get("extraction"), "stream extraction")
                ordinal = int(stream["ordinal"])
                start = int(stream["start"])
                end = int(stream["end"])
                if ordinal != stream_count:
                    errors.append("stream ordinals are not complete and ordered")
                if stream_count < len(index_catalog.ranges):
                    expected_stream = index_catalog.ranges[stream_count]
                    expected_page_ids = index_catalog.page_ids_by_stream[stream_count]
                    if (ordinal, start, end, int(stream["first_page_id"])) != (
                        expected_stream.ordinal,
                        expected_stream.start,
                        expected_stream.end,
                        expected_stream.first_page_id,
                    ):
                        errors.append(f"stream {ordinal} does not match the source index range")
                    if int(stream_index["pages"]) != len(expected_page_ids):
                        errors.append(f"stream {ordinal} indexed-page count mismatch")
                    page_id_hasher = CanonicalPageIdHasher()
                    for page_id in expected_page_ids:
                        page_id_hasher.update(page_id)
                    if stream_index.get("page_ids_sha256") != page_id_hasher.hexdigest():
                        errors.append(f"stream {ordinal} indexed page-ID hash mismatch")
                if previous_end is not None and start != previous_end + 1:
                    errors.append("stream byte ranges are not contiguous")
                if int(segment["bytes"]) != end - start + 1:
                    errors.append(f"stream {ordinal} byte accounting mismatch")
                if not isinstance(segment.get("sha256"), str) or len(segment["sha256"]) != 64:
                    errors.append(f"stream {ordinal} has an invalid source hash")
                pages = int(stream_extraction["pages_seen"])
                documents = int(stream_extraction["documents_emitted"])
                dropped = int(stream_extraction["pages_dropped"])
                if documents + dropped != pages:
                    errors.append(f"stream {ordinal} page accounting mismatch")
                stream_pages += pages
                stream_documents += documents
                stream_dropped += dropped
                stream_drop_counts.update(
                    cast(Mapping[str, int], stream_extraction.get("drop_counts", {}))
                )
                previous_end = end
                stream_count += 1
        if stream_count != source.get("stream_count"):
            errors.append("stream ledger count mismatch")
        if stream_count != len(index_catalog.ranges):
            errors.append("stream ledger does not match the source index stream count")
        if stream_count != artifact_meta["streams"].get("records"):
            errors.append("stream artifact record count mismatch")
        if previous_end != int(source["dump_bytes"]) - 1:
            errors.append("stream ledger does not reach the end of the dump")
        checks.append("complete contiguous stream ledger")

        split_raw = _mapping(manifest.get("split"), "split")
        if split_raw.get("strategy") != "page_id_sha256_basis_points":
            errors.append("unsupported full-dump split strategy")
        split = SplitConfig(
            train_bps=int(split_raw["train_bps"]),
            validation_bps=int(split_raw["validation_bps"]),
            test_bps=int(split_raw["test_bps"]),
            seed=str(split_raw["seed"]),
        )
        documents_path = artifact_paths["documents"]
        parquet = pq.ParquetFile(documents_path)
        if parquet.schema_arrow != DOCUMENT_SCHEMA:
            errors.append("document Parquet schema mismatch")
        content_hasher = CanonicalDocumentHasher()
        document_page_id_hasher = CanonicalPageIdHasher()
        expected_attribution_hasher = hashlib.sha256()
        previous_page_id = -1
        document_count = 0
        exact_groups: dict[str, tuple[str, int]] = {}
        cross_split_hashes: set[str] = set()
        duplicate_hashes: set[str] = set()
        residual_markup_count = 0
        residual_markup_examples: list[int] = []
        for batch in parquet.iter_batches(batch_size=512):
            for row in cast(list[dict[str, Any]], batch.to_pylist()):
                page_id = int(row["page_id"])
                if page_id <= previous_page_id:
                    errors.append("document page IDs are not strictly increasing and unique")
                    break
                previous_page_id = page_id
                if row["split"] != assign_split(page_id, split):
                    errors.append(f"page {page_id} has a non-deterministic split")
                if hashlib.sha256(str(row["text"]).encode()).hexdigest() != row["text_sha256"]:
                    errors.append(f"page {page_id} text SHA-256 mismatch")
                if any(
                    pattern.search(str(row["text"])) is not None
                    for pattern in _RESIDUAL_MARKUP_PATTERNS
                ):
                    residual_markup_count += 1
                    if len(residual_markup_examples) < 20:
                        residual_markup_examples.append(page_id)
                content_hasher.update(row)
                document_page_id_hasher.update(page_id)
                offer_near_sample(near_heap, row, near_sample_size)
                offer_review_candidate(review_heaps, row)
                attribution = {field: row[field] for field in _ATTRIBUTION_FIELDS}
                _attribution_hash_update(expected_attribution_hasher, attribution)
                text_hash = str(row["text_sha256"])
                prior = exact_groups.get(text_hash)
                if prior is None:
                    exact_groups[text_hash] = (str(row["split"]), page_id)
                else:
                    duplicate_hashes.add(text_hash)
                    if prior[0] != row["split"]:
                        cross_split_hashes.add(text_hash)
                document_count += 1
        if residual_markup_count:
            errors.append(
                f"{residual_markup_count} documents retain structural markup; "
                f"example page IDs: {residual_markup_examples}"
            )
        if document_count != artifact_meta["documents"].get("records"):
            errors.append("document artifact record count mismatch")
        if document_count != extraction.get("documents_emitted"):
            errors.append("extraction document count mismatch")
        if content_hasher.hexdigest() != artifacts.get("documents_content_sha256"):
            errors.append("canonical document content hash mismatch")
        checks.append("streamed document schema, identity, content, and splits")

        attribution_path = artifact_paths["attribution"]
        attribution_parquet = pq.ParquetFile(attribution_path)
        if attribution_parquet.schema_arrow != ATTRIBUTION_SCHEMA:
            errors.append("attribution Parquet schema mismatch")
        observed_attribution_hasher = hashlib.sha256()
        attribution_count = 0
        for batch in attribution_parquet.iter_batches(batch_size=512):
            for row in cast(list[dict[str, Any]], batch.to_pylist()):
                _attribution_hash_update(observed_attribution_hasher, row)
                attribution_count += 1
        attribution_digest = observed_attribution_hasher.hexdigest()
        if attribution_count != document_count:
            errors.append("attribution record count mismatch")
        if attribution_digest != expected_attribution_hasher.hexdigest():
            errors.append("attribution rows do not match document provenance")
        if attribution_digest != artifacts.get("attribution_content_sha256"):
            errors.append("attribution content hash mismatch")
        checks.append("revision-level attribution linkage")

        dropped_count = 0
        dropped_page_id_hasher = CanonicalPageIdHasher()
        observed_drop_counts: Counter[str] = Counter()
        with artifact_paths["dropped_pages"].open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                dropped_row = _mapping(json.loads(line), "dropped page")
                dropped_page_id_hasher.update(int(dropped_row["page_id"]))
                reason = str(dropped_row.get("reason"))
                if reason not in _DROP_REASONS:
                    errors.append(f"unknown dropped-page reason: {reason}")
                observed_drop_counts[reason] += 1
                dropped_count += 1
        if dropped_count != artifact_meta["dropped_pages"].get("records"):
            errors.append("dropped-page artifact record count mismatch")
        if dropped_count != extraction.get("pages_dropped"):
            errors.append("extraction dropped-page count mismatch")
        if document_count + dropped_count != extraction.get("pages_seen"):
            errors.append("not every full-dump input page is accounted for")
        if (stream_pages, stream_documents, stream_dropped) != (
            int(extraction["pages_seen"]),
            int(extraction["documents_emitted"]),
            int(extraction["pages_dropped"]),
        ):
            errors.append("stream-ledger totals do not match extraction totals")
        declared_drop_counts = {
            str(key): int(value)
            for key, value in _mapping(extraction.get("drop_counts"), "drop_counts").items()
        }
        if dict(observed_drop_counts) != {
            key: value for key, value in declared_drop_counts.items() if value
        }:
            errors.append("dropped-page reasons do not match extraction totals")
        if stream_drop_counts != observed_drop_counts:
            errors.append("stream drop-reason totals do not match the merged ledger")
        checks.append("full-dump page and exclusion accounting")

        decisions_parquet = pq.ParquetFile(artifact_paths["page_decisions"])
        if decisions_parquet.schema_arrow != PAGE_DECISION_SCHEMA:
            errors.append("page-decision Parquet schema mismatch")
        expected_decisions = iter(
            (ordinal, page_id)
            for ordinal, page_ids in enumerate(index_catalog.page_ids_by_stream)
            for page_id in page_ids
        )
        decision_document_hasher = CanonicalPageIdHasher()
        decision_dropped_hasher = CanonicalPageIdHasher()
        decision_count = 0
        decision_documents = 0
        decision_dropped = 0
        decision_drop_counts: Counter[str] = Counter()
        for batch in decisions_parquet.iter_batches(batch_size=1024):
            for row in cast(list[dict[str, Any]], batch.to_pylist()):
                try:
                    expected_ordinal, expected_page_id = next(expected_decisions)
                except StopIteration:
                    errors.append("page-decision ledger has pages absent from the source index")
                    expected_ordinal, expected_page_id = -1, -1
                page_id = int(row["page_id"])
                if (int(row["stream_ordinal"]), page_id) != (
                    expected_ordinal,
                    expected_page_id,
                ):
                    errors.append("page-decision order does not equal the source index")
                decision = row["decision"]
                reason = row["reason"]
                if decision == "emitted" and reason is None:
                    decision_document_hasher.update(page_id)
                    decision_documents += 1
                elif decision == "dropped" and reason in _DROP_REASONS:
                    decision_dropped_hasher.update(page_id)
                    decision_drop_counts[str(reason)] += 1
                    decision_dropped += 1
                else:
                    errors.append(f"invalid page decision for page {page_id}")
                decision_count += 1
        try:
            next(expected_decisions)
        except StopIteration:
            pass
        else:
            errors.append("page-decision ledger omits pages from the source index")
        if decision_count != artifact_meta["page_decisions"].get("records"):
            errors.append("page-decision artifact record count mismatch")
        if decision_count != index_catalog.page_count:
            errors.append("page-decision count does not equal the indexed page count")
        if (decision_documents, decision_dropped) != (document_count, dropped_count):
            errors.append("page-decision totals do not match emitted and dropped artifacts")
        if decision_document_hasher.hexdigest() != document_page_id_hasher.hexdigest():
            errors.append("emitted page decisions do not match document page IDs")
        if decision_dropped_hasher.hexdigest() != dropped_page_id_hasher.hexdigest():
            errors.append("dropped page decisions do not match dropped-page IDs")
        if decision_drop_counts != observed_drop_counts:
            errors.append("page-decision reasons do not match dropped-page reasons")
        checks.append("exact source-index to page-decision equality")

        exact = _mapping(audit.get("exact_duplicates"), "exact duplicate audit")
        if audit.get("documents_content_sha256") != content_hasher.hexdigest():
            errors.append("corpus audit is not linked to document content")
        if int(exact["duplicate_groups"]) != len(duplicate_hashes):
            errors.append("exact duplicate-group audit mismatch")
        if int(exact["cross_split_groups"]) != len(cross_split_hashes):
            errors.append("cross-split duplicate audit mismatch")
        if int(audit.get("page_id_split_intersection_count", -1)) != 0:
            errors.append("page identities intersect across splits")
        observed_near = near_duplicate_probe(near_heap, near_sample_size)
        if dict(declared_near) != observed_near:
            errors.append("near-duplicate sample audit is not reproducible from documents")
        checks.append("exhaustive exact and declared near-duplicate audit")

        review_candidates = [
            cast(dict[str, Any], json.loads(line))
            for line in artifact_paths["semantic_review_candidates"]
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        review_count = len(review_candidates)
        if review_count != artifact_meta["semantic_review_candidates"].get("records"):
            errors.append("semantic review candidate count mismatch")
        expected_review_candidates = selected_review_candidates(review_heaps)
        if review_candidates != expected_review_candidates:
            errors.append("semantic review candidates are not the pre-registered sample")
        semantic_plan = _mapping(audit.get("semantic_review"), "semantic review plan")
        expected_semantic_plan = {
            "method": "lowest SHA-256 semantic-review-v1 page scores per length stratum",
            "strata": list(REVIEW_STRATA),
            "candidates_per_stratum": REVIEW_PER_STRATUM,
            "candidate_count": len(expected_review_candidates),
            "inference_boundary": "reviewed sample only; not population accuracy",
        }
        if dict(semantic_plan) != expected_semantic_plan:
            errors.append("semantic review plan does not match the pre-registered contract")
        checks.append("semantic review candidate packet")

        tokenization_raw = artifacts.get("tokenization")
        if tokenization_raw is not None:
            tokenization = _mapping(tokenization_raw, "tokenization")
            tokenizer_path = _artifact_path(root, tokenization.get("tokenizer_path"))
            if not tokenizer_path.is_file():
                errors.append("missing tokenizer artifact")
            else:
                if tokenizer_path.stat().st_size != tokenization.get("tokenizer_bytes"):
                    errors.append("tokenizer byte count mismatch")
                if sha256_file(tokenizer_path) != tokenization.get("tokenizer_sha256"):
                    errors.append("tokenizer SHA-256 mismatch")
            dtype_name = tokenization.get("dtype")
            if dtype_name not in _DTYPES:
                errors.append("unsupported token dtype")
            else:
                dtype = np.dtype(_DTYPES[cast(str, dtype_name)])
                context_length = int(tokenization["context_length"])
                vocab_size = int(tokenization["vocab_size"])
                shards = tokenization.get("shards")
                if not isinstance(shards, list):
                    raise ValueError("token shards must be an array")
                for raw_shard in shards:
                    shard = _mapping(raw_shard, "token shard")
                    path = _artifact_path(root, shard.get("path"))
                    if not path.is_file():
                        errors.append(f"missing token shard: {path.name}")
                        continue
                    expected_bytes = int(shard["sequences"]) * context_length * dtype.itemsize
                    if path.stat().st_size != expected_bytes or expected_bytes != shard.get(
                        "bytes"
                    ):
                        errors.append(f"token shard size mismatch: {path.name}")
                    if sha256_file(path) != shard.get("sha256"):
                        errors.append(f"token shard SHA-256 mismatch: {path.name}")
                    values = np.memmap(path, mode="r", dtype=dtype)
                    if values.size and int(np.max(values)) >= vocab_size:
                        errors.append(f"out-of-vocabulary token id: {path.name}")
                smoke = _mapping(artifacts.get("training_smoke"), "training smoke")
                if float(smoke["loss_after"]) >= float(smoke["loss_before"]):
                    errors.append("tiny-model training smoke did not reduce loss")
                if not _artifact_path(root, smoke["shard"]).is_file():
                    errors.append("training smoke references a missing shard")
                checks.append("all token shards and tiny-model loader smoke")
    except (KeyError, OSError, TypeError, ValueError, WikiMLError, json.JSONDecodeError) as exc:
        errors.append(f"validation could not complete: {exc}")
    return ValidationReport(tuple(checks), tuple(errors))
