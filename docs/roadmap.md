# Roadmap

## v0.1 — bounded vertical slice

- One exact multistream byte range.
- Page accounting, Parquet, optional token shards, and mechanical validation.
- Offline CI fixtures plus a scheduled live-source smoke test.

## v0.2 — resumable multi-stream build

- Iterator over all indexed streams with bounded concurrency.
- Atomic per-stream checkpoints and idempotent restart.
- Deterministic merge independent of worker completion order.
- Failure-injection tests.

## v0.3 — corpus quality evidence

- Stratified extraction review harness and versioned labeled fixtures.
- Exact and near-duplicate evaluation with pre-registered acceptance criteria.
- Loader adapters and a tiny-model training smoke test.

## v1.0 — qualified release

- Complete Simple English and English Wikipedia run evidence.
- Reproduction from a second environment and external review.
- Data card, attribution artifacts, operational runbook, and versioned schemas.

Roadmap entries are proposals, not implemented capabilities.
