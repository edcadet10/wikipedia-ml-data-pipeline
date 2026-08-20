# Validation and acceptance policy

## What v0.1 checks exhaustively

- Manifest schema version and confined relative artifact paths.
- File byte counts and SHA-256 hashes.
- Exact Parquet schema, row count, unique page IDs, and canonical content hash.
- Recomputed page-level split assignment.
- Emitted-plus-dropped page accounting.
- Token shard sizes, hashes, integer dtype, and vocabulary bounds.

## What v0.1 does not establish

Passing mechanical validation does not establish semantic cleaning accuracy, full-dump
reliability, near-duplicate removal, absence of every train/test phrase overlap, model
quality, or legal compliance.

## Pre-registered full-dump acceptance gate

The claim “full-dump reliable” remains false until one release candidate satisfies all
of the following without an unresolved critical finding:

1. Use a dated dump and verify Wikimedia's published checksum before processing.
2. Account for every indexed page as emitted or excluded with a declared reason.
3. Match canonical content hashes across clean containers and worker counts.
4. Recover from injected termination with output identical to an uninterrupted run.
5. Observe zero page-ID intersection across splits and run declared duplicate probes.
6. Scan every shard for shape and token bounds; complete a small-model loader smoke test.
7. Review a pre-registered, stratified extraction sample against revision-matched renders;
   report only the observed sample result.
8. Complete independent code, data-card, attribution, and human licensing reviews.

Any failed condition blocks that claim. Passing a finite sample never becomes a claim
about an unobserved population tail.
