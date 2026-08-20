"""Small immutable value objects shared across pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DropReason(StrEnum):
    """Why a parsed page was intentionally excluded."""

    EMPTY_TEXT = "empty_text"
    INVALID_PAGE = "invalid_page"
    NON_ARTICLE_NAMESPACE = "non_article_namespace"
    REDIRECT = "redirect"


@dataclass(frozen=True, slots=True)
class Document:
    """A normalized latest-revision article and its provenance."""

    wiki: str
    page_id: int
    revision_id: int
    revision_timestamp: str
    title: str
    url: str
    text: str
    text_sha256: str
    split: str = ""
    license: str = "CC-BY-SA-4.0"


@dataclass(frozen=True, slots=True)
class DroppedPage:
    """A page exclusion retained for accounting and audit."""

    page_id: int | None
    title: str
    reason: DropReason


@dataclass(frozen=True, slots=True)
class ExtractionBatch:
    """Documents and exclusions observed in one compressed stream."""

    pages_seen: int
    documents: tuple[Document, ...]
    dropped: tuple[DroppedPage, ...]


@dataclass(frozen=True, slots=True)
class StreamRange:
    """Inclusive byte range for one independently compressed bzip2 stream."""

    ordinal: int
    start: int
    end: int
    first_page_id: int

    @property
    def length(self) -> int:
        """Return the exact number of bytes in the inclusive range."""

        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class SplitConfig:
    """Stable basis-point thresholds for document-level splits."""

    train_bps: int = 9800
    validation_bps: int = 100
    test_bps: int = 100
    seed: str = "wikiml-v1"

    def __post_init__(self) -> None:
        values = (self.train_bps, self.validation_bps, self.test_bps)
        if any(value < 0 for value in values):
            raise ValueError("split basis points cannot be negative")
        if sum(values) != 10_000:
            raise ValueError("split basis points must total 10,000")


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """Integrity metadata for one emitted file."""

    path: str
    sha256: str
    bytes: int
    records: int


@dataclass(frozen=True, slots=True)
class TokenShardSummary:
    """Integrity metadata for one fixed-length binary token shard."""

    path: str
    sha256: str
    bytes: int
    sequences: int
    split: str


@dataclass(frozen=True, slots=True)
class TokenizationSummary:
    """Tokenizer contract and all emitted binary shards."""

    tokenizer_sha256: str
    vocab_size: int
    eos_token_id: int
    dtype: str
    context_length: int
    sequences_per_shard: int
    dropped_tail_tokens: dict[str, int]
    shards: tuple[TokenShardSummary, ...]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Exhaustive mechanical validation result."""

    checks: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Return true only when no validation error was observed."""

        return not self.errors
