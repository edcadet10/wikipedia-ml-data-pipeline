"""Deterministic, bounded corpus-audit and semantic-review sampling."""

from __future__ import annotations

import hashlib
import heapq
import re
from dataclasses import dataclass
from typing import Any

_WORD = re.compile(r"\w+", flags=re.UNICODE)

REVIEW_STRATA = (
    "short_lt_500_chars",
    "medium_500_to_5000_chars",
    "long_gt_5000_chars",
)
REVIEW_PER_STRATUM = 10


@dataclass(frozen=True, slots=True)
class NearSample:
    """Bounded text retained for the declared near-duplicate probe."""

    page_id: int
    split: str
    text_sha256: str
    text: str


def offer_near_sample(
    heap: list[tuple[int, int, NearSample]], row: dict[str, Any], sample_size: int
) -> None:
    """Retain the lowest deterministic page-hash scores in a bounded heap."""

    page_id = int(row["page_id"])
    score = int.from_bytes(hashlib.sha256(f"near-v1:{page_id}".encode()).digest()[:8], "big")
    sample = NearSample(
        page_id=page_id,
        split=str(row["split"]),
        text_sha256=str(row["text_sha256"]),
        text=str(row["text"])[:8_000],
    )
    item = (-score, -page_id, sample)
    if len(heap) < sample_size:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def _simhash(text: str) -> int | None:
    words = _WORD.findall(text.casefold())[:1_000]
    if len(words) < 20:
        return None
    weights = [0] * 64
    for index in range(len(words) - 2):
        shingle = "\x1f".join(words[index : index + 3]).encode()
        value = int.from_bytes(hashlib.sha256(shingle).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return result


def near_duplicate_probe(
    heap: list[tuple[int, int, NearSample]], requested_size: int
) -> dict[str, Any]:
    """Run the declared finite SimHash probe over a deterministic sample."""

    samples = [entry[2] for entry in sorted(heap, reverse=True)]
    fingerprints = [(_simhash(sample.text), sample) for sample in samples]
    usable = [(value, sample) for value, sample in fingerprints if value is not None]
    buckets: dict[tuple[int, int], list[tuple[int, NearSample]]] = {}
    examined = 0
    pairs = 0
    cross_split = 0
    examples: list[dict[str, Any]] = []
    for value, sample in usable:
        candidates: dict[int, tuple[int, NearSample]] = {}
        for band in range(4):
            key = (band, (value >> (band * 16)) & 0xFFFF)
            for prior_value, prior in buckets.get(key, []):
                candidates[prior.page_id] = (prior_value, prior)
        for prior_value, prior in candidates.values():
            examined += 1
            if sample.text_sha256 == prior.text_sha256:
                continue
            distance = (value ^ prior_value).bit_count()
            if distance <= 3:
                pairs += 1
                if sample.split != prior.split:
                    cross_split += 1
                if len(examples) < 20:
                    examples.append(
                        {
                            "page_ids": sorted((prior.page_id, sample.page_id)),
                            "splits": sorted((prior.split, sample.split)),
                            "hamming_distance": distance,
                        }
                    )
        for band in range(4):
            key = (band, (value >> (band * 16)) & 0xFFFF)
            buckets.setdefault(key, []).append((value, sample))
    return {
        "method": (
            "deterministic page-hash sample; first 8,000 characters; "
            "first 1,000 case-folded words; word-3-gram 64-bit SimHash"
        ),
        "requested_sample_size": requested_size,
        "observed_sample_size": len(samples),
        "eligible_documents": len(usable),
        "hamming_distance_threshold": 3,
        "candidate_pairs_examined": examined,
        "near_duplicate_pairs_observed": pairs,
        "cross_split_pairs_observed": cross_split,
        "examples": examples,
        "inference_boundary": "observed sample only; not a corpus-wide near-duplicate count",
    }


def review_stratum(text: str) -> str:
    """Assign one of the pre-registered extraction-length strata."""

    length = len(text)
    if length < 500:
        return REVIEW_STRATA[0]
    if length <= 5_000:
        return REVIEW_STRATA[1]
    return REVIEW_STRATA[2]


def new_review_heaps() -> dict[str, list[tuple[int, int, dict[str, Any]]]]:
    """Create empty bounded heaps for every pre-registered stratum."""

    return {stratum: [] for stratum in REVIEW_STRATA}


def offer_review_candidate(
    heaps: dict[str, list[tuple[int, int, dict[str, Any]]]], row: dict[str, Any]
) -> None:
    """Offer a document to the deterministic ten-per-stratum review sample."""

    stratum = review_stratum(str(row["text"]))
    score = int.from_bytes(
        hashlib.sha256(f"semantic-review-v1:{row['page_id']}".encode()).digest()[:8], "big"
    )
    candidate = {
        "stratum": stratum,
        "page_id": int(row["page_id"]),
        "revision_id": int(row["revision_id"]),
        "revision_timestamp": row["revision_timestamp"],
        "title": row["title"],
        "url": row["url"],
        "oldid_url": f"{row['url']}&oldid={row['revision_id']}",
        "text_sha256": row["text_sha256"],
        "extracted_text": row["text"],
        "human_label": None,
        "human_notes": None,
    }
    heap = heaps[stratum]
    item = (-score, -int(row["page_id"]), candidate)
    if len(heap) < REVIEW_PER_STRATUM:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def selected_review_candidates(
    heaps: dict[str, list[tuple[int, int, dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Return candidates in the canonical file order."""

    return [
        candidate
        for stratum in sorted(heaps)
        for _score, _page, candidate in sorted(heaps[stratum], reverse=True)
    ]
