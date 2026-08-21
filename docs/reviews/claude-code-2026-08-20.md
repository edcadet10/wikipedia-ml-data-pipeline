# Independent engineering review: Claude Code

## Scope and reviewer

- Reviewer: Claude Code CLI 2.1.237, `sonnet` alias (reported canonical model:
  Claude Sonnet 5)
- Method: read-only, hostile review of the implementation, tests, validation,
  attribution, checkpointing, and claim boundaries
- Review dates: 2026-08-20 through 2026-08-21
- Contract-6 exact commit: `25d9f53fb508eb8860bc5511b548a153198c899e`
- Final contract-7 implementation commit:
  `4a12315edf8778fe7f1493c2d9ba4f33c05a0b24`

Claude Code is an independent implementation reviewer for this gate, not the human
semantic reviewer or the human licensing reviewer. Runs that exhausted their budget
without returning a disposition are recorded for transparency but are not counted as
review evidence.

## Review chain

| Stage | CLI-reported cost | Disposition | Finding and disposition |
|---|---:|---|---|
| Initial orchestration audit | $1.0603340 | BLOCK | Critical: overlapping work/output trees made `--discard-work` capable of deleting a published dataset. Fixed with symmetric disjoint-tree validation and tests. |
| Page-accounting re-audit | $0.8745530 | BLOCK | High: a missing malformed XML page ID was not representable in the exclusion ledger. Fixed by binding decisions to ordered source-index identity and testing positional mismatch. |
| Budget-capped broad attempt | $2.6982700 | No verdict; excluded | Reached its budget without a result. |
| Full hostile diff audit | $1.1475464 | BLOCK | High: nested level-3+ boilerplate survived. Medium: malformed identity could be checked too late; post-publication cleanup could falsely report build failure. All fixed and tested. |
| Focused re-audit | $0.4668944 | CONDITIONAL PASS | Confirmed all three preceding findings resolved; no new critical/high finding. |
| Contract-4 reference audit | $0.4489864 | BLOCK | Medium: an unquoted-URL reference form could be treated as self-closing and leak content; validator lacked a corresponding invariant. Fixed by regex ordering, validator enforcement, and artifact mutation tests. |
| Contract-4 follow-up | $0.3677574 | CONDITIONAL PASS | Confirmed both findings resolved; no critical/high finding. |
| Contract-5 no-space `refname=` audit | $0.3244874 | CONDITIONAL PASS | Confirmed the malformed-tag regex and checked that it did not match ordinary `references`/`reformation` names. An explicit references-tag regression was added. |
| Contract-6 structural-residue audit | $1.1394165 | BLOCK | Critical: an inline table-attribute fragment could evade both extraction and validation. Medium: the validator reported only the first residual page. Fixed with distinct safe-line cleanup and inline-residue detection, full count/sample reporting, and extractor/validator regressions. |
| Contract-6 follow-up attempt | $0.9341878 | No verdict; excluded | Reached its budget while attempting denied shell checks. |
| Contract-6 scoped attempt | $2.5467240 | No verdict; excluded | Reached its budget without a disposition. |
| Contract-6 exact-commit audit | $1.6988313 | PASS | Read-only hostile review of `25d9f53fb508eb8860bc5511b548a153198c899e`; no critical/high finding. Reported bounded-decompression, XML-hardening, and manual-container-gate observations as non-blocking. |
| GitHub CodeQL PR scan | n/a | BLOCK | Two high-severity `py/bad-tag-filter` alerts: extraction and independent validation recognized `-->` but not the HTML parser's alternate `--!>` comment end form. Fixed in both paths, covered by regressions, and invalidated as pipeline contract 7. |
| Contract-7 exact-commit audit | $1.1024604 | PASS | Read-only hostile review of `4a12315edf8778fe7f1493c2d9ba4f33c05a0b24`; verified both regex paths, targeted regressions, independent validation, contract-7 checkpoint invalidation, and the broader critical/high categories. No critical/high finding. |

GitHub CI, dependency review, CodeQL analysis, and the CodeQL pull-request gate all
passed on the same implementation commit. The final CodeQL gate reported no remaining
alert from the two contract-6 `py/bad-tag-filter` findings.

## Contract-6 resolution evidence

The contract-6 fix keeps two cases separate:

1. A line that begins with a declared table attribute is a removable table row.
2. A table attribute embedded within prose has an ambiguous boundary, so the page is
   excluded as `markup_residue` rather than partially rewritten.

The independent validator duplicates the residual-pattern semantics, counts every
offending document, and records up to 20 example page IDs. Unit tests mutate a built
Parquet artifact with an inline fragment and separately exercise extractor ledgering.
The full local check executed after the fix reported 120 passing tests, 90.46% coverage,
strict typing/lint/format success, valid wheel/sdist metadata, and no known vulnerable
locked dependency.

## Residual risks

- Per-stream and index bzip2 decompression is checksum-bound but does not impose an
  explicit decompressed-size ceiling.
- XML extraction uses the standard-library parser rather than a hardened third-party
  parser. The published-checksum trust boundary reduces, but does not erase, parser DoS
  risk.
- The pinned container's unprivileged entry point is checked in CI, while the complete
  zero-checkpoint reproduction remains a per-candidate manual acceptance experiment.
- Structural-residue rules are intentionally conservative and can exclude legitimate
  articles that discuss wiki/HTML syntax. Exclusion is preferred to guessing malformed
  boundaries, and the exact count is reported.
- Passing mechanical checks cannot establish semantic quality outside the selected
  review sample.
- This engineering review does not determine licensing compliance or provide legal
  advice.
