"""Minimal zero-copy reader for fixed-length token shards."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

_DTYPES: dict[str, str] = {"uint16-le": "<u2", "uint32-le": "<u4"}


class TokenShard:
    """Memory-map one validated token shard as [sequence, position]."""

    def __init__(self, path: Path, *, dtype: str, context_length: int) -> None:
        if dtype not in _DTYPES:
            raise ValueError(f"unsupported token dtype: {dtype}")
        if context_length <= 0:
            raise ValueError("context_length must be positive")
        dtype_value = np.dtype(_DTYPES[dtype])
        itemsize = dtype_value.itemsize
        sequence_bytes = context_length * itemsize
        if path.stat().st_size % sequence_bytes:
            raise ValueError("shard size is not divisible by one sequence")
        flat = np.memmap(path, mode="r", dtype=dtype_value)
        self._data: NDArray[Any] = flat.reshape((-1, context_length))

    def __len__(self) -> int:
        return int(self._data.shape[0])

    def __getitem__(self, index: int) -> NDArray[Any]:
        return cast(NDArray[Any], self._data[index])
