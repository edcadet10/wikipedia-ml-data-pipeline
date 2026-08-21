# Human review protocol

This protocol keeps semantic and licensing decisions separate from automated pipeline
validation. Reviewers should not edit `semantic-review-candidates.jsonl`; it is an
immutable, hash-declared sample selected before labels are observed.

## Semantic extraction review

### Sampling

The pipeline selects the ten lowest `SHA-256("semantic-review-v1:<page_id>")` scores in
each extracted-text length stratum:

- `short_lt_500_chars`
- `medium_500_to_5000_chars`
- `long_gt_5000_chars`

All 30 candidates must be reviewed. Open each `oldid_url` and confirm that the rendered
revision ID is the one recorded in the candidate. If a revision cannot be inspected,
label it `unreviewable`; this blocks the gate rather than being silently omitted.

### Labels

Record one decision per page in a separate UTF-8 JSON Lines file:

```json
{"page_id":123,"revision_id":456,"reviewer":"GitHub @handle","reviewed_at":"YYYY-MM-DD","label":"acceptable","notes":"Faithful prose; no material markup residue."}
```

Allowed labels are:

- `acceptable`: central article prose is readable and materially faithful; no disruptive
  markup, navigation, table fragments, reference lists, or template noise remains.
- `minor_issue`: localized noise or omission that does not materially change the sample's
  usefulness. Describe it concretely.
- `major_issue`: fabricated/joined text, dominant markup or metadata, materially wrong
  omission, wrong revision/page, or text that is not usable article prose.
- `unreviewable`: the pinned source revision cannot be inspected or compared.

The semantic gate passes only when all 30 decisions are present, revision IDs match,
and neither `major_issue` nor `unreviewable` occurs. Report counts by stratum and label,
every note, reviewer identity/date, candidate-file SHA-256, and the narrow conclusion:
“No major issue was observed in this 30-document pre-registered sample.” Do not claim a
population accuracy rate.

## Licensing and attribution review

A domain-appropriate human reviews `DATA_CARD.md`, `DATA_LICENSE.md`, `manifest.json`,
`attribution.parquet`, and a representative set of revision-pinned source pages. The
review must explicitly answer:

1. Is the source snapshot and applicable Wikimedia/Wikipedia license identified?
2. Does the proposed distribution provide a reasonable author-attribution path via the
   page/revision history?
3. Are the license notice, ShareAlike requirement, and transformation/change notice
   carried with the corpus?
4. Are page-specific or imported-content additional terms handled or clearly bounded?
5. Are token shards, metadata, mirrors, access terms, and downstream redistribution
   covered by the stated plan?
6. Is the decision `approve`, `approve_with_conditions`, or `reject`, and what conditions
   or unresolved risks remain?

Record reviewer identity/role, date, candidate release/commit, reviewed artifact hashes,
answers, and decision in `docs/reviews/`. An AI-generated review may find questions but
cannot be the human sign-off. No corpus should be published from this project's GitHub
release workflow until the review is recorded and its conditions are met.

## Independent engineering review

The reviewer receives the acceptance criteria and is asked to falsify, not endorse, the
implementation. At minimum, inspect source acquisition, index/XML alignment, checkpoint
reuse, output/work path safety, deterministic merge, split logic, token serialization,
validator independence, attribution linkage, and claim wording. Record tool/reviewer,
scope, exact commit, findings by severity, fixes, residual risks, and final disposition.
