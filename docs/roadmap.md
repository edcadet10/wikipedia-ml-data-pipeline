# Roadmap

## Delivered in v0.1 — bounded vertical slice

- Exact one-stream HTTP range acquisition and page accounting.
- Parquet documents, optional token shards, and mechanical validation.
- Offline CI fixtures and a scheduled live-source smoke test.

## Delivered in v0.2 — complete dated-dump candidate

- Bounded full-dump acquisition with published-checksum verification.
- Multi-process stream execution, atomic checkpoints, and idempotent restart.
- Deterministic merge independent of worker completion order.
- Exact source-index-to-page-decision accounting.
- Attribution, data-card, duplicate-audit, and stratified-review artifacts.
- Streaming tokenization, exhaustive shard checks, and tiny-model training smoke.
- Failure injection, corruption tests, clean-container reproduction procedure, and
  independent review records.

Human review results and dated empirical qualification evidence are release records,
not capabilities inferred from this roadmap.

## Next level — v0.3

- Land independently labeled semantic fixtures and regression cases discovered by review.
- Add measured throughput/RSS benchmarks across worker counts and representative hardware.
- Add content-level split mitigation policies, then evaluate their utility/recall costs.
- Support sharded Wikimedia dumps larger than one physical XML file.
- Add optional language-aware extraction profiles and quality filters with ablations.
- Publish loader adapters and a small Transformer training experiment with a reproducible
  configuration, while keeping dataset correctness separate from model quality.

## Longer term — v1.0

- Qualify current Simple English and English Wikipedia release candidates.
- Reproduce on a second operator's infrastructure and incorporate external review.
- Version and migrate schemas with compatibility fixtures.
- Add operational observability, capacity planning, and a distributed object-store
  checkpoint backend before claiming cluster-scale operation.
- Establish a documented governance process for release sign-off and corpus takedowns.
