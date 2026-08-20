# Contributing

Thanks for helping make Wikipedia-derived ML data easier to inspect and reproduce.
Contributions are welcome from first-time contributors and experienced data/ML
engineers alike.

## Good first contributions

- Add a synthetic XML fixture for an extraction edge case.
- Propose a validation that can falsify an existing claim.
- Improve documentation without broadening the current claim scope.
- Add a loader adapter behind an optional dependency.
- Submit a reproducible benchmark result using the benchmark issue form.

## Development setup

```bash
git clone https://github.com/edcadet10/wikipedia-ml-data-pipeline.git
cd wikipedia-ml-data-pipeline
uv sync --locked --all-groups
uv run pre-commit install
make check
```

Tests must not depend on live network services. Use synthetic content that you wrote
or content whose redistribution terms are explicit. The scheduled live smoke workflow
owns the small real-network probe.

## Pull-request contract

1. Open or reference an issue for behavior changes.
2. Add a falsifying test before changing a reliability or correctness claim.
3. Preserve deterministic ordering, explicit provenance, and page-level split isolation.
4. Update the data contract and changelog when public output changes.
5. Run `make check` and include the observed result in the pull request.

Performance pull requests must include raw commands, the source artifact identity,
hardware/software environment, repeated raw samples, correctness evidence, and the
specific observation that would overturn the claim. Results describe only the tested
environment.

## Reviews

At least one approving review is expected for non-trivial changes. Security-sensitive,
licensing, schema, and claims-policy changes should be reviewed by a domain-appropriate
human; an AI review can supplement but not replace that review.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
