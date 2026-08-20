# Data contract

The public contract is versioned by `manifest.json.schema_version`. Breaking field or
semantic changes require a schema-version increment.

## Documents

`documents.parquet` uses the exact field order and Arrow types below.

| Field | Arrow type | Invariant |
|---|---|---|
| `wiki` | string | Wikimedia database identifier |
| `page_id` | int64 | Unique within the artifact |
| `revision_id` | int64 | Latest revision represented by the dump |
| `revision_timestamp` | string | Source timestamp, retained verbatim |
| `title` | string | Source page title |
| `url` | string | Attribution path using `?curid=` |
| `text` | string | NFC-normalized extracted text |
| `text_sha256` | string | SHA-256 of UTF-8 `text` |
| `split` | string | Hash-derived document split |
| `license` | string | Source-content license identifier |

## Exclusions

`dropped-pages.jsonl` contains `page_id`, `title`, and one stable reason: `redirect`,
`non_article_namespace`, `empty_text`, or `invalid_page`. Emitted plus dropped counts
must exactly equal pages observed in the stream.

## Token shards

Token files are headerless contiguous little-endian integers. The manifest supplies
dtype, context length, vocabulary size, tokenizer SHA-256, EOS ID, split, sequence
count, byte count, and file hash. A sequence never crosses a document split; documents
inside one split may be packed together with EOS between them. A final incomplete
sequence is dropped and its token count is recorded per split.

## Manifest

The manifest names the exact source URLs, HTTP identity headers, index and segment
hashes, inclusive stream range, configuration, extraction counts, all emitted files,
and the scope of claims that have and have not been validated.
