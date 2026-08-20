from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wikiml.loader import TokenShard
from wikiml.models import Document
from wikiml.tokenize import write_token_shards


def _document(page_id: int, split: str, text: str) -> Document:
    return Document(
        wiki="simplewiki",
        page_id=page_id,
        revision_id=page_id + 100,
        revision_timestamp="2026-08-20T12:00:00Z",
        title=f"Page {page_id}",
        url=f"https://simple.wikipedia.org/?curid={page_id}",
        text=text,
        text_sha256="0" * 64,
        split=split,
    )


def test_token_shards_are_fixed_length_and_memory_mappable(
    tmp_path: Path, tokenizer_factory: Callable[[], Path]
) -> None:
    tokenizer_path = tokenizer_factory()
    documents = (
        _document(1, "train", "Alpha is useful . Second line ."),
        _document(2, "train", "Café has text ."),
    )

    summary = write_token_shards(
        documents,
        tokenizer_path=tokenizer_path,
        eos_token_id=1,
        output_dir=tmp_path,
        context_length=4,
        sequences_per_shard=2,
    )

    assert summary.dtype == "uint16-le"
    assert summary.shards
    shard = TokenShard(
        tmp_path / summary.shards[0].path,
        dtype=summary.dtype,
        context_length=summary.context_length,
    )
    assert len(shard) == summary.shards[0].sequences
    assert shard[0].shape == (4,)


def test_tokenization_rejects_invalid_eos(
    tmp_path: Path, tokenizer_factory: Callable[[], Path]
) -> None:
    with pytest.raises(ValueError, match="vocabulary"):
        write_token_shards(
            (replace(_document(1, "train", "Alpha")),),
            tokenizer_path=tokenizer_factory(),
            eos_token_id=999,
            output_dir=tmp_path,
            context_length=4,
            sequences_per_shard=2,
        )


def test_loader_rejects_misaligned_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.bin"
    path.write_bytes(b"123")
    with pytest.raises(ValueError, match="not divisible"):
        TokenShard(path, dtype="uint16-le", context_length=2)


@pytest.mark.parametrize(
    ("dtype", "context", "message"),
    [("float32", 2, "unsupported"), ("uint16-le", 0, "positive")],
)
def test_loader_rejects_unsupported_contract(
    tmp_path: Path, dtype: str, context: int, message: str
) -> None:
    path = tmp_path / "tokens.bin"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match=message):
        TokenShard(path, dtype=dtype, context_length=context)


@pytest.mark.parametrize(
    ("context", "per_shard"),
    [(0, 1), (1, 0)],
)
def test_tokenization_rejects_invalid_shapes(
    tmp_path: Path,
    tokenizer_factory: Callable[[], Path],
    context: int,
    per_shard: int,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        write_token_shards(
            (),
            tokenizer_path=tokenizer_factory(),
            eos_token_id=1,
            output_dir=tmp_path,
            context_length=context,
            sequences_per_shard=per_shard,
        )
