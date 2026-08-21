# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Complete dated-dump `wikiml build` command with bounded acquisition and Wikimedia
  published-checksum verification.
- Identity-bound atomic stream checkpoints, deterministic multi-process merge, injected
  failure testing, and safe disjoint output/work-directory enforcement.
- Complete source-index, stream, page-decision, attribution, data-card, exact-duplicate,
  near-duplicate, and pre-registered semantic-review artifacts.
- Streaming tokenization with a bundled hash-pinned tokenizer and deterministic tiny-
  model training smoke.
- Schema-v2 streaming validation and adversarial corruption tests.
- Pinned reproducibility container and human semantic/licensing review protocol.

### Changed

- Extraction reparses normalized output, removes declared non-prose namespace links,
  tables, residual table/formatting controls, parsed/malformed reference blocks, and
  relative-depth boilerplate sections, and ledgers structurally ambiguous pages as
  `markup_residue` instead of guessing their boundaries.
- Missing or malformed XML page IDs are ledgered using the corresponding validated
  source-index identity; page-order disagreement is a hard failure.
- Package version is now 0.2.0 and the full-pipeline semantic contract is version 6.

### Fixed

- Preserve the final multistream pages by removing only the terminal MediaWiki wrapper.
- Reject overlapping work/output directory trees so `--discard-work` cannot erase a
  successfully published dataset.
- Publish the dataset root as `0755` rather than retaining `mkdtemp`'s owner-only `0700`
  mode, allowing a different unprivileged UID to consume a read-only mount.

## [0.1.0] - 2026-08-19

### Added

- Public bounded vertical slice for one Wikimedia multistream segment.
- Exact HTTP range enforcement, page accounting, Parquet output, optional token shards,
  integrity validation, and memory-mapped token loading.
- Reproducible development environment, CI/CD, security policy, and contribution workflow.
