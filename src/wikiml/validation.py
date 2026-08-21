"""Exhaustive mechanical checks for one emitted dataset directory."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow.parquet as pq

from wikiml.manifest import read_manifest
from wikiml.models import SplitConfig, ValidationReport
from wikiml.split import assign_split
from wikiml.storage import DOCUMENT_SCHEMA, canonical_document_hash, sha256_file

_DTYPES: dict[str, str] = {"uint16-le": "<u2", "uint32-le": "<u4"}


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


def validate_dataset(root: Path) -> ValidationReport:
    """Validate provenance-linked files, logical rows, splits, and token bounds."""

    checks: list[str] = []
    errors: list[str] = []
    try:
        manifest = read_manifest(root)
        if manifest.get("schema_version") == 2:
            from wikiml.full_validation import validate_full_dataset

            return validate_full_dataset(root, manifest)
        if manifest.get("schema_version") != 1:
            errors.append("unsupported manifest schema_version")
            return ValidationReport(tuple(checks), tuple(errors))
        checks.append("manifest schema")

        extraction = _mapping(manifest.get("extraction"), "extraction")
        artifacts = _mapping(manifest.get("artifacts"), "artifacts")
        documents_meta = _mapping(artifacts.get("documents"), "documents artifact")
        dropped_meta = _mapping(artifacts.get("dropped_pages"), "dropped-pages artifact")

        documents_path = _artifact_path(root, documents_meta.get("path"))
        dropped_path = _artifact_path(root, dropped_meta.get("path"))
        for label, path, metadata in (
            ("documents", documents_path, documents_meta),
            ("dropped pages", dropped_path, dropped_meta),
        ):
            if not path.is_file():
                errors.append(f"missing {label} artifact")
                continue
            if path.stat().st_size != metadata.get("bytes"):
                errors.append(f"{label} byte count mismatch")
            if sha256_file(path) != metadata.get("sha256"):
                errors.append(f"{label} SHA-256 mismatch")
        if errors:
            return ValidationReport(tuple(checks), tuple(errors))
        checks.append("artifact hashes")

        table = pq.read_table(documents_path)
        if table.schema != DOCUMENT_SCHEMA:
            errors.append("document Parquet schema mismatch")
        rows = table.to_pylist()
        if len(rows) != documents_meta.get("records"):
            errors.append("document row count mismatch")
        if len(rows) != extraction.get("documents_emitted"):
            errors.append("extraction document count mismatch")
        if canonical_document_hash(rows) != artifacts.get("documents_content_sha256"):
            errors.append("canonical document content hash mismatch")
        checks.append("document schema and content")

        page_ids = [int(row["page_id"]) for row in rows]
        if len(page_ids) != len(set(page_ids)):
            errors.append("duplicate page_id in document artifact")
        split_raw = _mapping(manifest.get("split"), "split")
        split_config = SplitConfig(
            train_bps=int(split_raw["train_bps"]),
            validation_bps=int(split_raw["validation_bps"]),
            test_bps=int(split_raw["test_bps"]),
            seed=str(split_raw["seed"]),
        )
        for row in rows:
            if hashlib.sha256(str(row["text"]).encode()).hexdigest() != row["text_sha256"]:
                errors.append(f"page {row['page_id']} text SHA-256 mismatch")
            expected = assign_split(int(row["page_id"]), split_config)
            if row["split"] != expected:
                errors.append(f"page {row['page_id']} has a non-deterministic split")
        checks.append("document identity and split isolation")

        dropped_lines = [
            line for line in dropped_path.read_text(encoding="utf-8").splitlines() if line
        ]
        if len(dropped_lines) != dropped_meta.get("records"):
            errors.append("dropped-page row count mismatch")
        if len(dropped_lines) != extraction.get("pages_dropped"):
            errors.append("extraction dropped-page count mismatch")
        if len(rows) + len(dropped_lines) != extraction.get("pages_seen"):
            errors.append("not every input page is accounted for")
        checks.append("input page accounting")

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
                checks.append("token shard shape, hash, and vocabulary bounds")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        errors.append(f"validation could not complete: {exc}")

    return ValidationReport(tuple(checks), tuple(errors))
