"""Stable document-level partitioning."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from wikiml.models import Document, SplitConfig


def assign_split(page_id: int, config: SplitConfig) -> str:
    """Assign a page to one split without depending on input order."""

    material = f"{config.seed}:{page_id}".encode()
    bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 10_000
    if bucket < config.train_bps:
        return "train"
    if bucket < config.train_bps + config.validation_bps:
        return "validation"
    return "test"


def partition_documents(
    documents: tuple[Document, ...], config: SplitConfig
) -> tuple[Document, ...]:
    """Attach deterministic splits and return a canonical page-id ordering."""

    return tuple(
        replace(document, split=assign_split(document.page_id, config))
        for document in sorted(documents, key=lambda item: item.page_id)
    )
