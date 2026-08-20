from __future__ import annotations

import pytest

from wikiml.models import Document, SplitConfig
from wikiml.split import assign_split, partition_documents


def _document(page_id: int) -> Document:
    return Document(
        wiki="simplewiki",
        page_id=page_id,
        revision_id=page_id + 100,
        revision_timestamp="2026-08-20T12:00:00Z",
        title=f"Page {page_id}",
        url=f"https://simple.wikipedia.org/?curid={page_id}",
        text="text",
        text_sha256="0" * 64,
    )


def test_split_is_stable_and_order_independent() -> None:
    config = SplitConfig(train_bps=6000, validation_bps=2000, test_bps=2000, seed="test")
    documents = (_document(9), _document(1), _document(5))

    first = partition_documents(documents, config)
    second = partition_documents(tuple(reversed(documents)), config)

    assert first == second
    assert [item.page_id for item in first] == [1, 5, 9]
    assert all(item.split == assign_split(item.page_id, config) for item in first)


@pytest.mark.parametrize("train,validation,test", [(9900, 100, 100), (-1, 1, 10_000)])
def test_split_config_rejects_invalid_totals(train: int, validation: int, test: int) -> None:
    with pytest.raises(ValueError):
        SplitConfig(train_bps=train, validation_bps=validation, test_bps=test)


def test_assign_split_reaches_every_configured_partition() -> None:
    config = SplitConfig(train_bps=1, validation_bps=1, test_bps=9998, seed="branches")
    observed = {assign_split(page_id, config) for page_id in range(50_000)}

    assert observed == {"train", "validation", "test"}
