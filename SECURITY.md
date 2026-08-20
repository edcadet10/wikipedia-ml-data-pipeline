# Security policy

## Supported versions

Security fixes target the latest released minor version and the `main` branch.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use GitHub's private
[security-advisory form](https://github.com/edcadet10/wikipedia-ml-data-pipeline/security/advisories/new).
Include the affected version, reproduction steps, impact, and any proposed mitigation.

You should receive acknowledgment within five business days. No bounty is promised.

## Security boundaries

The pipeline treats dump bytes, XML, wikitext, tokenizer files, and manifests as
untrusted input. Network responses are size-bounded; range responses are exact; output
paths are confined during validation; generated data is never executed. These controls
reduce risk but do not establish that all parsers or native dependencies are free of
vulnerabilities.
