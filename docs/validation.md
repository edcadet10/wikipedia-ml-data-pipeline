# Validation and acceptance policy

## Exhaustive mechanical validation

For schema-v2 full builds, `wikiml validate` independently streams and checks:

- manifest schema, pipeline contract, dated snapshot, completed dump status, confined
  artifact paths, byte counts, and SHA-256 hashes;
- the bundled multistream index, complete contiguous stream ledger, published/observed
  source hashes, stream-to-index counts, and ordered page-ID hashes;
- exact Parquet schemas, strictly increasing unique document IDs, text hashes, stable
  split assignment, absence of declared structural-markup residue, and canonical
  logical-content hash;
- attribution equality with document provenance and its independent content hash;
- every exclusion reason and equality among stream, extraction, exclusion, source-index,
  and page-decision totals;
- exact equality between every ordered source-index ID and its emitted/dropped decision;
- the exhaustive exact-content audit and deterministic finite near-duplicate probe;
- exact reproduction of the pre-registered semantic-review candidate packet;
- the bundled tokenizer identity, every token shard's size/hash/range, and decreasing
  tiny-model smoke loss when tokenization is enabled.

Passing those checks establishes internal consistency for the declared transformation.
It does not establish semantic accuracy, population-wide near-duplicate absence, useful
model quality, or legal compliance.

## Pre-registered full acceptance gate

The release-level claim “qualified complete-dump build” remains false until one release
candidate satisfies every condition below without an unresolved critical finding:

1. Use a dated dump and verify Wikimedia's published checksum before processing.
2. Account for every indexed page as emitted or excluded with a declared reason.
3. Match every content artifact hash and canonical content hash across a clean pinned
   container and a host run using different worker counts.
4. Inject termination after 100 serial checkpoints; publish no partial output; resume
   those exact checkpoints with four workers; match the clean uninterrupted output.
5. Observe zero page-ID intersection across splits, exhaustively count exact normalized-
   text groups, and run the declared deterministic near-duplicate probe. Report findings;
   do not silently redefine the gate after observing them.
6. Scan every token shard for shape and bounds and complete the tiny-model loader/training
   smoke test with decreasing loss.
7. Have a human review all 30 pre-registered, length-stratified extraction candidates
   against revision-pinned source renders. Any `major_issue` blocks semantic acceptance;
   report only the observed sample result.
8. Complete independent engineering review of code, tests, data card, and attribution;
   resolve every critical/high finding or document why it is non-blocking.
9. Obtain a human licensing review of the data card and redistribution/attribution plan.
   AI review does not satisfy this gate, and repository acceptance is not legal advice.

The exact review procedure and decision schema are in
[the review protocol](review-protocol.md). A finite passing sample is never generalized
to an unobserved population tail.

## Failure severity

- `critical`: source identity, page accounting, split isolation, artifact integrity,
  atomic publication, or license/attribution failure; always blocks acceptance.
- `high`: reproducibility failure, material extraction corruption, unsafe resource use,
  or incomplete validation; blocks until fixed or explicitly narrowed out of scope.
- `medium`: maintainability or evidence weakness that does not invalidate the observed
  run; must have an owner and disposition.
- `low`: polish or optional hardening; may remain with transparent documentation.

## Evidence rules

Acceptance records must include the commit, lockfile hash, source URLs and published
checksums, container base and built-image digests, exact commands, exit codes, counts,
content/artifact hashes, test and audit results, independent-review findings, reviewer
identity, decision, and date. Raw corpora and credentials must not be committed.

If code, dependencies, extraction semantics, tokenizer bytes, source snapshot, or split
configuration changes, the affected empirical gates must be rerun. A package-version or
pipeline-contract change invalidates incompatible checkpoints.
