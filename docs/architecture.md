# Architecture

## Design objective

Produce auditable model-data artifacts without hiding source identity, page decisions,
split policy, tokenizer identity, interruption behavior, or unvalidated claims.

## Trust boundaries

The network, compressed bytes, XML, wikitext, tokenizer JSON, checkpoint directory,
and existing manifests are untrusted. The pipeline therefore applies bounded downloads,
published-checksum verification, bzip2/XML failures, exact index-to-XML page matching,
path confinement, local-only tokenizer loading, cryptographic identities, output staging,
and post-write validation.

SHA-1 is used only to compare Wikimedia artifacts with Wikimedia's published dump
manifest. SHA-256 protects local artifact identity and canonical logical content.

## Complete-dump stages

1. Resolve a dated snapshot's status, SHA-1 manifest, dump, and compressed multistream
   index. Reject incomplete jobs, mutable snapshot names, unexpected sizes, and checksum
   mismatches.
2. Parse the complete index into contiguous byte ranges and the exact, strictly ordered
   page IDs assigned to each range.
3. Bind the source, split policy, package version, and pipeline contract version into a
   checkpoint identity.
4. Read one local byte range per process job, decompress it, align every XML page with
   its index identity, extract documents, assign stable splits, and atomically commit a
   checkpoint only after revalidation.
5. On restart, re-hash each source segment and checkpoint artifact, then re-check its
   schema, counts, page decisions, split assignments, and build identity before reuse.
6. Merge checkpoints by stream ordinal and ascending page ID. Worker completion order
   cannot affect the logical or byte-level output.
7. Stream the merge into document, attribution, exclusion, stream, and page-decision
   artifacts. Recompute exact-content groups and bounded deterministic samples.
8. Optionally stream documents through the bundled hash-pinned tokenizer into fixed-
   length little-endian shards and execute a deterministic tiny-model training smoke.
9. Write the manifest last, independently stream-validate every declared artifact, and
   atomically rename the staging directory into place only when validation succeeds.

## Checkpoint lifecycle

Checkpoint directories are named by zero-padded stream ordinal. A worker writes to a
temporary sibling, validates it, and renames it atomically. Stale staging directories
are deleted during resume; completed checkpoints are never trusted merely because they
exist. The output and persistent work directory must be disjoint, including either path
being nested beneath the other.

The build identity deliberately excludes worker count so a serial interrupted run can
resume in parallel. It includes the source metadata, split configuration, package
version, and an explicit pipeline contract version so extraction-semantic changes cannot
silently reuse older checkpoints.

## Determinism

Parquet settings, JSON serialization, row order, review sampling, duplicate sampling,
token packing, and tiny-model initialization are fixed. Each artifact has a byte hash.
A separate length-delimited canonical row hash captures logical document content and
does not depend on Parquet container metadata.

The clean-container acceptance comparison requires all artifact identities and logical
content to match. Execution metadata such as worker count and checkpoint reuse is
expected to differ and is compared separately.

## Resource bounds

- Index, checksum, status, and full-dump downloads have explicit byte ceilings.
- Workers receive one compressed stream at a time; the whole uncompressed dump is never
  materialized in memory.
- Checkpoint Parquet and final Parquet are consumed in bounded record batches.
- Near-duplicate and semantic-review selection use bounded heaps.
- Tokenization reads Parquet batches and flushes finite shard buffers.

An individual compressed stream is still decompressed in one worker allocation. The
configured 64 GiB default applies to the compressed dump, not to temporary work,
Parquet output, or token shards; operators must provision disk separately.

## Claim boundary

Mechanical validation establishes only that the declared transformation was performed
consistently and that the emitted artifacts match their manifest. Semantic suitability,
population-wide near-duplicate behavior, model utility, and license compliance remain
separate empirical or human-review questions.
