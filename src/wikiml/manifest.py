"""Stable JSON manifest serialization."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from wikiml import __version__
from wikiml.models import ArtifactSummary, SplitConfig, TokenizationSummary


def build_manifest(
    *,
    source: dict[str, Any],
    pages_seen: int,
    drop_counts: dict[str, int],
    split_config: SplitConfig,
    documents: ArtifactSummary,
    documents_content_sha256: str,
    dropped: ArtifactSummary,
    tokenization: TokenizationSummary | None,
) -> dict[str, Any]:
    """Build the versioned public manifest without wall-clock-dependent fields."""

    return {
        "schema_version": 1,
        "project": {
            "name": "wikipedia-ml-data-pipeline",
            "version": __version__,
            "scope": "one_multistream_segment",
        },
        "source": source,
        "extraction": {
            "pages_seen": pages_seen,
            "documents_emitted": documents.records,
            "pages_dropped": dropped.records,
            "drop_counts": dict(sorted(drop_counts.items())),
        },
        "split": asdict(split_config),
        "artifacts": {
            "documents": asdict(documents),
            "documents_content_sha256": documents_content_sha256,
            "dropped_pages": asdict(dropped),
            "tokenization": None if tokenization is None else asdict(tokenization),
        },
        "claims": {
            "full_dump_validated": False,
            "semantic_extraction_validated": False,
            "tested_scope": "the selected multistream segment only",
        },
    }


def write_manifest(manifest: dict[str, Any], output_dir: Path) -> Path:
    """Write the manifest last, using an atomic same-filesystem replacement."""

    target = output_dir / "manifest.json"
    temporary = output_dir / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def read_manifest(root: Path) -> dict[str, Any]:
    """Load a manifest object and reject non-object JSON."""

    value: object = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return cast(dict[str, Any], value)
