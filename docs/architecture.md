# Architecture

## Design objective

Produce auditable model-data artifacts without hiding source identity, page decisions,
split policy, tokenizer identity, or unvalidated claims.

## Trust boundaries

The network, compressed bytes, XML, wikitext, tokenizer JSON, and an existing dataset
manifest are untrusted. v0.1 applies bounded downloads, exact `Content-Range` checks,
bzip2/XML parse failures, local-only tokenizer loading, output staging, path confinement,
cryptographic hashes, and post-write validation.

## Stages

1. Resolve unique bzip2 stream offsets from the Wikimedia multistream index.
2. Request one inclusive range and reject full-body or mismatched responses.
3. Decompress and incrementally consume complete `<page>` elements.
4. Keep latest-revision namespace-zero articles; ledger every exclusion.
5. Normalize text and assign the whole page to one deterministic split.
6. Write document Parquet and optional tokenizer-specific binary shards.
7. Write the manifest last and validate the staged directory.
8. Atomically publish the staged directory only after validation passes.

## Deliberate constraints

v0.1 collects one compressed stream into memory. This keeps the initial implementation
small enough to audit and is not a full-dump architecture. Full-dump work must introduce
bounded stream iteration, checkpoints, idempotent resume, shard-level commits, and
cross-worker canonical ordering without weakening the current contracts.

Parquet byte hashes are recorded for artifact integrity. A separate canonical row hash
captures logical document content because container metadata can change across PyArrow
versions; dependency versions remain locked for byte-level reproduction.
