"""Verification of human decisions over the immutable semantic-review packet."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, cast

from wikiml.errors import ValidationError
from wikiml.manifest import read_manifest
from wikiml.models import ValidationReport
from wikiml.sampling import REVIEW_PER_STRATUM, REVIEW_STRATA
from wikiml.storage import sha256_file
from wikiml.validation import validate_dataset

_LABELS = {"acceptable", "minor_issue", "major_issue", "unreviewable"}
_BLOCKING_LABELS = {"major_issue", "unreviewable"}


def _jsonl(path: Path, kind: str) -> list[dict[str, Any]]:
    try:
        rows = [
            cast(dict[str, Any], json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"could not read {kind} JSON Lines: {exc}") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise ValidationError(f"{kind} must contain JSON objects")
    return rows


def _require_integer(row: dict[str, Any], field: str, kind: str) -> int:
    value = row.get(field)
    if type(value) is not int or value < 0:
        raise ValidationError(f"{kind} has an invalid {field}")
    return value


def evaluate_semantic_review(dataset: Path, decisions_path: Path) -> dict[str, Any]:
    """Validate complete human labels without mutating the source candidate packet."""

    report: ValidationReport = validate_dataset(dataset)
    if not report.ok:
        raise ValidationError("dataset validation failed: " + "; ".join(report.errors))
    root = dataset.resolve()
    manifest = read_manifest(root)
    try:
        candidate_relative = manifest["artifacts"]["semantic_review_candidates"]["path"]
    except (KeyError, TypeError) as exc:
        raise ValidationError("dataset has no semantic review candidate artifact") from exc
    if not isinstance(candidate_relative, str):
        raise ValidationError("semantic review candidate path is invalid")
    candidate_path = (root / candidate_relative).resolve()
    if not candidate_path.is_relative_to(root):
        raise ValidationError("semantic review candidate path escapes the dataset")

    candidates = _jsonl(candidate_path, "candidate")
    decisions = _jsonl(decisions_path, "decision")
    expected_total = len(REVIEW_STRATA) * REVIEW_PER_STRATUM
    if len(candidates) != expected_total:
        raise ValidationError(
            f"candidate packet has {len(candidates)} rows; full review requires {expected_total}"
        )

    candidate_by_page: dict[int, dict[str, Any]] = {}
    candidate_strata: Counter[str] = Counter()
    for candidate in candidates:
        page_id = _require_integer(candidate, "page_id", "candidate")
        _require_integer(candidate, "revision_id", "candidate")
        stratum = candidate.get("stratum")
        if stratum not in REVIEW_STRATA:
            raise ValidationError(f"candidate {page_id} has an invalid stratum")
        if page_id in candidate_by_page:
            raise ValidationError(f"candidate page {page_id} is duplicated")
        candidate_by_page[page_id] = candidate
        candidate_strata[str(stratum)] += 1
    if candidate_strata != Counter({stratum: REVIEW_PER_STRATUM for stratum in REVIEW_STRATA}):
        raise ValidationError("candidate packet is not ten-per-stratum")

    decision_by_page: dict[int, dict[str, Any]] = {}
    labels: Counter[str] = Counter()
    labels_by_stratum: dict[str, Counter[str]] = {stratum: Counter() for stratum in REVIEW_STRATA}
    reviewers: set[str] = set()
    review_dates: set[str] = set()
    blocking_page_ids: list[int] = []
    for decision in decisions:
        page_id = _require_integer(decision, "page_id", "decision")
        revision_id = _require_integer(decision, "revision_id", "decision")
        current_candidate = candidate_by_page.get(page_id)
        if current_candidate is None:
            raise ValidationError(f"decision references non-candidate page {page_id}")
        if page_id in decision_by_page:
            raise ValidationError(f"decision for page {page_id} is duplicated")
        if revision_id != current_candidate["revision_id"]:
            raise ValidationError(f"decision for page {page_id} has the wrong revision")
        label = decision.get("label")
        if label not in _LABELS:
            raise ValidationError(f"decision for page {page_id} has an invalid label")
        reviewer = decision.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValidationError(f"decision for page {page_id} has no reviewer")
        reviewed_at = decision.get("reviewed_at")
        if not isinstance(reviewed_at, str):
            raise ValidationError(f"decision for page {page_id} has no review date")
        try:
            date.fromisoformat(reviewed_at)
        except ValueError as exc:
            raise ValidationError(
                f"decision for page {page_id} has an invalid review date"
            ) from exc
        notes = decision.get("notes")
        if not isinstance(notes, str):
            raise ValidationError(f"decision for page {page_id} has invalid notes")
        if label != "acceptable" and not notes.strip():
            raise ValidationError(f"non-acceptable decision for page {page_id} requires notes")

        decision_by_page[page_id] = decision
        labels[str(label)] += 1
        labels_by_stratum[str(current_candidate["stratum"])][str(label)] += 1
        reviewers.add(reviewer.strip())
        review_dates.add(reviewed_at)
        if label in _BLOCKING_LABELS:
            blocking_page_ids.append(page_id)

    missing = sorted(candidate_by_page.keys() - decision_by_page.keys())
    if missing:
        raise ValidationError(f"decisions omit {len(missing)} candidate pages")
    passed = not blocking_page_ids
    conclusion = (
        "No major issue was observed in this 30-document pre-registered sample."
        if passed
        else "The semantic review gate is blocked by major or unreviewable cases."
    )
    return {
        "passed": passed,
        "candidate_file": candidate_relative,
        "candidate_file_sha256": sha256_file(candidate_path),
        "candidate_count": len(candidates),
        "decision_file_sha256": sha256_file(decisions_path),
        "decision_count": len(decisions),
        "label_counts": {label: labels.get(label, 0) for label in sorted(_LABELS)},
        "labels_by_stratum": {
            stratum: {label: labels_by_stratum[stratum].get(label, 0) for label in sorted(_LABELS)}
            for stratum in REVIEW_STRATA
        },
        "blocking_page_ids": sorted(blocking_page_ids),
        "reviewers": sorted(reviewers),
        "reviewed_at_dates": sorted(review_dates),
        "conclusion": conclusion,
        "inference_boundary": "reviewed sample only; not population accuracy",
    }
