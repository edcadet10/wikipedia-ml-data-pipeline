"""Tokenizer-pinned, fixed-length little-endian binary shard creation."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from tokenizers import Tokenizer

from wikiml.loader import TokenShard
from wikiml.models import Document, TokenizationSummary, TokenShardSummary
from wikiml.storage import sha256_file

_SPLITS = ("train", "validation", "test")


def _tokenizer_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_tokenizer_artifact(tokenizer_path: Path, output_dir: Path) -> Path:
    target = output_dir / "tokenizer.json"
    if tokenizer_path.resolve() != target.resolve():
        shutil.copyfile(tokenizer_path, target)
    return target


def _token_contract(
    tokenizer_path: Path, eos_token_id: int
) -> tuple[Tokenizer, int, np.dtype[Any], str]:
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    vocab_size = tokenizer.get_vocab_size(with_added_tokens=True)
    if eos_token_id < 0 or eos_token_id >= vocab_size:
        raise ValueError("eos_token_id must belong to the tokenizer vocabulary")
    max_token_id = max(vocab_size - 1, eos_token_id)
    if max_token_id <= np.iinfo(np.uint16).max:
        return tokenizer, vocab_size, np.dtype("<u2"), "uint16-le"
    if max_token_id <= np.iinfo(np.uint32).max:
        return tokenizer, vocab_size, np.dtype("<u4"), "uint32-le"
    raise ValueError("tokenizer vocabulary does not fit in uint32")


def _write_shard(
    shard_ids: list[int],
    *,
    split: str,
    shard_index: int,
    dtype: np.dtype[Any],
    context_length: int,
    output_dir: Path,
) -> TokenShardSummary:
    path = output_dir / "tokens" / f"{split}-{shard_index:05d}.bin"
    array = np.asarray(shard_ids, dtype=dtype)
    path.write_bytes(array.tobytes(order="C"))
    return TokenShardSummary(
        path=path.relative_to(output_dir).as_posix(),
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        sequences=len(shard_ids) // context_length,
        split=split,
    )


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
    tokenizer, vocab_size, dtype, dtype_name = _token_contract(tokenizer_path, eos_token_id)
    tokenizer_artifact = _copy_tokenizer_artifact(tokenizer_path, output_dir)

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
            shards.append(
                _write_shard(
                    shard_ids,
                    split=split,
                    shard_index=shard_index,
                    dtype=dtype,
                    context_length=context_length,
                    output_dir=output_dir,
                )
            )

    return TokenizationSummary(
        tokenizer_path=tokenizer_artifact.name,
        tokenizer_sha256=_tokenizer_sha256(tokenizer_artifact),
        tokenizer_bytes=tokenizer_artifact.stat().st_size,
        vocab_size=vocab_size,
        eos_token_id=eos_token_id,
        dtype=dtype_name,
        context_length=context_length,
        sequences_per_shard=sequences_per_shard,
        dropped_tail_tokens=dropped_tail_tokens,
        shards=tuple(shards),
    )


def write_token_shards_from_parquet(
    documents_path: Path,
    *,
    tokenizer_path: Path,
    eos_token_id: int,
    output_dir: Path,
    context_length: int,
    sequences_per_shard: int,
    batch_size: int = 256,
) -> tuple[TokenizationSummary, dict[str, int | float | str]]:
    """Tokenize a Parquet corpus with bounded buffers and run a tiny-model smoke test."""

    if context_length <= 0:
        raise ValueError("context_length must be positive")
    if sequences_per_shard <= 0:
        raise ValueError("sequences_per_shard must be positive")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    tokenizer, vocab_size, dtype, dtype_name = _token_contract(tokenizer_path, eos_token_id)
    tokenizer_artifact = _copy_tokenizer_artifact(tokenizer_path, output_dir)
    token_dir = output_dir / "tokens"
    token_dir.mkdir()
    tokens_per_shard = context_length * sequences_per_shard
    buffers: dict[str, list[int]] = {split: [] for split in _SPLITS}
    shard_indices = dict.fromkeys(_SPLITS, 0)
    shards: list[TokenShardSummary] = []

    parquet = pq.ParquetFile(documents_path)
    for batch in parquet.iter_batches(batch_size=batch_size, columns=["text", "split"]):
        data = batch.to_pydict()
        texts = [str(value) for value in data["text"]]
        splits = [str(value) for value in data["split"]]
        encodings = tokenizer.encode_batch(texts, add_special_tokens=False)
        for split, encoding in zip(splits, encodings, strict=True):
            if split not in buffers:
                raise ValueError(f"document has unsupported split: {split}")
            if encoding.ids and max(encoding.ids) >= vocab_size:
                raise ValueError("tokenizer emitted an id outside its declared vocabulary")
            buffer = buffers[split]
            buffer.extend(encoding.ids)
            buffer.append(eos_token_id)
            while len(buffer) >= tokens_per_shard:
                shard_ids = buffer[:tokens_per_shard]
                del buffer[:tokens_per_shard]
                shard_index = shard_indices[split]
                shards.append(
                    _write_shard(
                        shard_ids,
                        split=split,
                        shard_index=shard_index,
                        dtype=dtype,
                        context_length=context_length,
                        output_dir=output_dir,
                    )
                )
                shard_indices[split] = shard_index + 1

    dropped_tail_tokens: dict[str, int] = {}
    for split in _SPLITS:
        buffer = buffers[split]
        usable = len(buffer) // context_length * context_length
        dropped_tail_tokens[split] = len(buffer) - usable
        if usable:
            shard_index = shard_indices[split]
            shards.append(
                _write_shard(
                    buffer[:usable],
                    split=split,
                    shard_index=shard_index,
                    dtype=dtype,
                    context_length=context_length,
                    output_dir=output_dir,
                )
            )

    summary = TokenizationSummary(
        tokenizer_path=tokenizer_artifact.name,
        tokenizer_sha256=_tokenizer_sha256(tokenizer_artifact),
        tokenizer_bytes=tokenizer_artifact.stat().st_size,
        vocab_size=vocab_size,
        eos_token_id=eos_token_id,
        dtype=dtype_name,
        context_length=context_length,
        sequences_per_shard=sequences_per_shard,
        dropped_tail_tokens=dropped_tail_tokens,
        shards=tuple(shards),
    )
    return summary, _tiny_bigram_smoke(summary, output_dir)


def _tiny_bigram_smoke(
    summary: TokenizationSummary, output_dir: Path
) -> dict[str, int | float | str]:
    """Prove that memory-mapped sequences can drive one finite optimization step."""

    if not summary.shards:
        raise ValueError("training smoke test requires at least one token shard")
    first = summary.shards[0]
    shard = TokenShard(
        output_dir / first.path,
        dtype=summary.dtype,
        context_length=summary.context_length,
    )
    if not len(shard):
        raise ValueError("training smoke test requires a non-empty token shard")
    flat = np.asarray(shard[0], dtype=np.int64)
    if flat.size < 2:
        raise ValueError("training smoke test requires at least two tokens")
    inputs = flat[:-1]
    targets = flat[1:]
    vocabulary = np.unique(np.concatenate((inputs, targets)))
    input_ids = np.searchsorted(vocabulary, inputs)
    target_ids = np.searchsorted(vocabulary, targets)
    weights = np.zeros((vocabulary.size, vocabulary.size), dtype=np.float64)

    def loss_and_gradient() -> tuple[float, np.ndarray[Any, Any]]:
        logits = weights[input_ids]
        logits -= np.max(logits, axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= np.sum(probabilities, axis=1, keepdims=True)
        loss = -float(np.mean(np.log(probabilities[np.arange(target_ids.size), target_ids])))
        probabilities[np.arange(target_ids.size), target_ids] -= 1.0
        probabilities /= target_ids.size
        gradient = np.zeros_like(weights)
        np.add.at(gradient, input_ids, probabilities)
        return loss, gradient

    loss_before, gradient = loss_and_gradient()
    weights -= gradient
    loss_after, _unused = loss_and_gradient()
    if not np.isfinite(loss_before) or not np.isfinite(loss_after) or loss_after >= loss_before:
        raise ValueError("tiny-model training smoke test did not reduce finite loss")
    return {
        "model": "numpy-bigram-one-step",
        "shard": first.path,
        "sequences_loaded": 1,
        "token_pairs": int(target_ids.size),
        "observed_vocabulary": int(vocabulary.size),
        "loss_before": round(loss_before, 12),
        "loss_after": round(loss_after, 12),
    }
