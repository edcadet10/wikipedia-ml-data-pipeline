# Data contract

The public artifact contract is versioned by `manifest.json.schema_version`. Complete-
dump outputs use schema version 2 and pipeline contract version 6. The small `probe`
command retains schema version 1 compatibility. Breaking field or semantic changes
require an appropriate version increment.

Contract version 6 reparses normalized output once to remove newly exposed balanced
constructs, safely removes residual formatting controls and table-attribute rows, and
excludes pages whose remaining structural markup has ambiguous boundaries. It retains
contract 5's malformed no-space `refname=` handling and invalidates checkpoints from all
earlier v0.2 candidates.

## Documents

`documents.parquet` uses this exact field order and Arrow types.

| Field | Arrow type | Invariant |
|---|---|---|
| `wiki` | string | Wikimedia database identifier |
| `page_id` | int64 | Unique, strictly increasing in a full artifact |
| `revision_id` | int64 | Latest revision represented by the dump |
| `revision_timestamp` | string | Source timestamp, retained verbatim |
| `title` | string | Source page title |
| `url` | string | Attribution path using `?curid=` |
| `text` | string | NFC-normalized extracted text |
| `text_sha256` | string | SHA-256 of UTF-8 `text` |
| `split` | string | Deterministic `train`, `validation`, or `test` |
| `license` | string | Declared source-content license identifier |

The canonical logical-content digest hashes each row's sorted, compact JSON encoding,
prefixed by its eight-byte big-endian length, in ascending `page_id` order.

The complete output directory is atomically published with mode `0755` so artifacts can
be traversed and read by a downstream unprivileged user. Individual files and nested
directories retain ordinary umask-derived read/traverse permissions.

## Extraction and exclusions

Only namespace-zero, non-redirect pages with a parsable revision and at least three
alphabetic Unicode words after normalization are emitted. Declared non-prose namespace
links, table/gallery/imagemap markup, parsed or malformed reference blocks, boilerplate
sections, templates, formatting controls, and residual table-row fragments are removed.
The normalized output is parsed a second time; pages are excluded rather than guessed at
when template, link, non-prose tag, transclusion, or table structure remains ambiguous.
Paragraph breaks are normalized and excessive blank lines collapsed.

`dropped-pages.jsonl` contains `page_id`, `title`, and exactly one stable reason:

| Reason | Meaning |
|---|---|
| `redirect` | XML or textual redirect |
| `non_article_namespace` | Page namespace is not zero |
| `empty_text` | No text remains after transformation |
| `insufficient_text` | Fewer than three alphabetic words remain |
| `markup_residue` | Ambiguous structural markup remains after the second parse |
| `invalid_page` | Required page or revision metadata is absent or malformed |

For complete builds, a malformed XML page ID is represented by the corresponding index
identity. Positional disagreement, extra XML pages, or missing XML pages is a hard error.

## Page decisions and source linkage

`page-decisions.parquet` has `page_id: int64`, `stream_ordinal: int32`,
`decision: string`, and nullable `reason: string`. Its rows exactly equal the ordered IDs
re-derived from the bundled `source-index.txt.bz2`. An emitted row has a null reason; a
dropped row has one of the reasons above.

`streams.jsonl` contains every contiguous source byte range, source-segment SHA-256,
index page count/hash, extraction counts, and checkpoint logical-content hash.

`attribution.parquet` repeats `wiki`, page and revision IDs, revision timestamp, title,
URL, and license for every emitted document. Its canonical hash and row-by-row equality
with document provenance are validated independently.

## Duplicate and review artifacts

`corpus-audit.json` contains:

- an exhaustive grouping by normalized-text SHA-256, including cross-split counts;
- zero page-identity intersection by construction and validator rederivation;
- a deterministic bounded sample for a declared 64-bit word-trigram SimHash probe;
- the deterministic semantic-review selection method and inference boundary.

`semantic-review-candidates.jsonl` selects ten documents from each of three extracted-
length strata using the lowest seeded SHA-256 page scores. It includes extracted text,
revision identity, and a revision-pinned URL. Empty human fields are part of the immutable
candidate packet; decisions belong in a separate review record.

## Token shards

Token files are headerless contiguous little-endian integers. The manifest supplies
dtype, context length, vocabulary size, bundled tokenizer path/SHA-256/bytes, EOS ID,
split, sequence count, byte count, and file hash. Documents never cross splits; documents
within one split may be packed together with EOS between them. A final incomplete
sequence is dropped and its token count is recorded per split.

The validator scans every shard for byte shape, hash, and vocabulary bounds. The build
also records a deterministic tiny NumPy bigram-training run whose loss must decrease.

## Manifest and claim fields

The manifest names exact source URLs and hashes, dump status and date, complete index
identity, configuration, execution metadata, extraction counts, every emitted artifact,
and explicit boolean claim fields. Build-time validation leaves human-dependent claim
fields false. Changing those flags requires a separate, reviewable qualification record;
the artifact validator does not infer legal or semantic acceptance.
