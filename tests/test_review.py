from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wikiml.errors import ValidationError
from wikiml.models import ValidationReport
from wikiml.review import evaluate_semantic_review
from wikiml.sampling import REVIEW_PER_STRATUM, REVIEW_STRATA


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


@pytest.fixture
def review_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, list[dict[str, Any]]]:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    page_id = 1
    for stratum in REVIEW_STRATA:
        for _index in range(REVIEW_PER_STRATUM):
            candidates.append(
                {
                    "page_id": page_id,
                    "revision_id": page_id + 100,
                    "stratum": stratum,
                }
            )
            decisions.append(
                {
                    "page_id": page_id,
                    "revision_id": page_id + 100,
                    "reviewer": "GitHub @reviewer",
                    "reviewed_at": "2026-08-20",
                    "label": "acceptable",
                    "notes": "",
                }
            )
            page_id += 1
    _write_jsonl(dataset / "semantic-review-candidates.jsonl", candidates)
    (dataset / "manifest.json").write_text("{}", encoding="utf-8")
    decision_path = tmp_path / "decisions.jsonl"
    _write_jsonl(decision_path, decisions)
    monkeypatch.setattr(
        "wikiml.review.validate_dataset",
        lambda _path: ValidationReport(("fixture",), ()),
    )
    monkeypatch.setattr(
        "wikiml.review.read_manifest",
        lambda _path: {
            "artifacts": {
                "semantic_review_candidates": {"path": "semantic-review-candidates.jsonl"}
            }
        },
    )
    return dataset, decision_path, decisions


def test_semantic_review_accepts_complete_nonblocking_packet(
    review_fixture: tuple[Path, Path, list[dict[str, Any]]],
) -> None:
    dataset, decisions_path, _decisions = review_fixture

    result = evaluate_semantic_review(dataset, decisions_path)

    assert result["passed"] is True
    assert result["candidate_count"] == 30
    assert result["label_counts"]["acceptable"] == 30
    assert result["blocking_page_ids"] == []


def test_semantic_review_reports_blocking_label(
    review_fixture: tuple[Path, Path, list[dict[str, Any]]],
) -> None:
    dataset, decisions_path, decisions = review_fixture
    decisions[0]["label"] = "major_issue"
    decisions[0]["notes"] = "Dominant table markup remains."
    _write_jsonl(decisions_path, decisions)

    result = evaluate_semantic_review(dataset, decisions_path)

    assert result["passed"] is False
    assert result["blocking_page_ids"] == [1]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows.pop(), "omit"),
        (lambda rows: rows.append(rows[0]), "duplicated"),
        (lambda rows: rows[0].__setitem__("revision_id", 999), "wrong revision"),
        (lambda rows: rows[0].__setitem__("label", "good"), "invalid label"),
        (lambda rows: rows[0].__setitem__("reviewer", ""), "no reviewer"),
        (lambda rows: rows[0].__setitem__("reviewed_at", "today"), "invalid review date"),
        (lambda rows: rows[0].__setitem__("notes", None), "invalid notes"),
        (
            lambda rows: (
                rows[0].__setitem__("label", "minor_issue"),
                rows[0].__setitem__("notes", ""),
            ),
            "requires notes",
        ),
    ],
)
def test_semantic_review_rejects_invalid_decisions(
    review_fixture: tuple[Path, Path, list[dict[str, Any]]],
    mutate: Callable[[list[dict[str, Any]]], object],
    message: str,
) -> None:
    dataset, decisions_path, decisions = review_fixture
    mutate(decisions)
    _write_jsonl(decisions_path, decisions)

    with pytest.raises(ValidationError, match=message):
        evaluate_semantic_review(dataset, decisions_path)


def test_semantic_review_requires_valid_dataset(
    review_fixture: tuple[Path, Path, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, decisions_path, _decisions = review_fixture
    monkeypatch.setattr(
        "wikiml.review.validate_dataset",
        lambda _path: ValidationReport((), ("tampered",)),
    )

    with pytest.raises(ValidationError, match="tampered"):
        evaluate_semantic_review(dataset, decisions_path)
