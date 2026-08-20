"""Tokenizer-pinned, fixed-length little-endian binary shard creation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from tokenizers import Tokenizer

from wikiml.models import Document, TokenizationSummary, TokenShardSummary
from wikiml.storage import sha256_file

_SPLITS = ("train", "validation", "test")


def _tokenizer_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_token_shards(
    documents: tuple[Document, ...],
    *,
    tokenizer_path: Path,
    eos_token_id: int,
    output_dir: Path,
    context_length: int,
    sequences_per_shard: int,
) -> TokenizationSummary:
    """Pack each document split separately, inserting EOS at document boundaries."""

    if context_length <= 0:
        raise ValueError("context_length must be positive")
    if sequences_per_shard <= 0:
        raise ValueError("sequences_per_shard must be positive")
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    vocab_size = tokenizer.get_vocab_size(with_added_tokens=True)
    if eos_token_id < 0 or eos_token_id >= vocab_size:
        raise ValueError("eos_token_id must belong to the tokenizer vocabulary")
    max_token_id = max(vocab_size - 1, eos_token_id)
    dtype: np.dtype[Any]
    if max_token_id <= np.iinfo(np.uint16).max:
        dtype = np.dtype("<u2")
        dtype_name = "uint16-le"
    elif max_token_id <= np.iinfo(np.uint32).max:
        dtype = np.dtype("<u4")
        dtype_name = "uint32-le"
    else:
        raise ValueError("tokenizer vocabulary does not fit in uint32")

    token_dir = output_dir / "tokens"
    token_dir.mkdir()
    shards: list[TokenShardSummary] = []
    dropped_tail_tokens: dict[str, int] = {}
    for split in _SPLITS:
        token_ids: list[int] = []
        for document in documents:
            if document.split != split:
                continue
            encoded = tokenizer.encode(document.text, add_special_tokens=False).ids
            if encoded and max(encoded) >= vocab_size:
                raise ValueError("tokenizer emitted an id outside its declared vocabulary")
            token_ids.extend(encoded)
            token_ids.append(eos_token_id)

        usable = len(token_ids) // context_length * context_length
        dropped_tail_tokens[split] = len(token_ids) - usable
        token_ids = token_ids[:usable]
        tokens_per_shard = context_length * sequences_per_shard
        for shard_index, start in enumerate(range(0, len(token_ids), tokens_per_shard)):
            shard_ids = token_ids[start : start + tokens_per_shard]
            path = token_dir / f"{split}-{shard_index:05d}.bin"
            array = np.asarray(shard_ids, dtype=dtype)
            path.write_bytes(array.tobytes(order="C"))
            shards.append(
                TokenShardSummary(
                    path=path.relative_to(output_dir).as_posix(),
                    sha256=sha256_file(path),
                    bytes=path.stat().st_size,
                    sequences=len(shard_ids) // context_length,
                    split=split,
                )
            )

    return TokenizationSummary(
        tokenizer_sha256=_tokenizer_sha256(tokenizer_path),
        vocab_size=vocab_size,
        eos_token_id=eos_token_id,
        dtype=dtype_name,
        context_length=context_length,
        sequences_per_shard=sequences_per_shard,
        dropped_tail_tokens=dropped_tail_tokens,
        shards=tuple(shards),
    )
