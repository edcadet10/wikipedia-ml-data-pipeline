"""Command-line interface with machine-readable output and explicit failure codes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from wikiml import __version__
from wikiml.errors import WikiMLError
from wikiml.manifest import read_manifest
from wikiml.models import SplitConfig
from wikiml.pipeline import ProbeConfig, run_probe
from wikiml.validation import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    """Create the public CLI grammar."""

    parser = argparse.ArgumentParser(
        prog="wikiml",
        description="Build auditable model-data artifacts from Wikimedia multistream dumps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="process one indexed bzip2 stream")
    probe.add_argument("--output", type=Path, required=True)
    probe.add_argument("--wiki", default="simplewiki")
    probe.add_argument("--snapshot", default="latest")
    probe.add_argument("--stream", type=int, default=0, dest="stream_ordinal")
    probe.add_argument("--base-url")
    probe.add_argument("--tokenizer-json", type=Path, dest="tokenizer_path")
    probe.add_argument("--eos-token-id", type=int)
    probe.add_argument("--context-length", type=int, default=1024)
    probe.add_argument("--sequences-per-shard", type=int, default=4096)
    probe.add_argument("--split-seed", default="wikiml-v1")
    probe.add_argument("--train-bps", type=int, default=9800)
    probe.add_argument("--validation-bps", type=int, default=100)
    probe.add_argument("--test-bps", type=int, default=100)

    validate = subparsers.add_parser("validate", help="verify every declared artifact")
    validate.add_argument("dataset", type=Path)

    inspect = subparsers.add_parser("inspect", help="print the complete dataset manifest")
    inspect.add_argument("dataset", type=Path)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Execute a CLI request and return a conventional process status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            split = SplitConfig(
                train_bps=args.train_bps,
                validation_bps=args.validation_bps,
                test_bps=args.test_bps,
                seed=args.split_seed,
            )
            manifest = run_probe(
                ProbeConfig(
                    output_dir=args.output,
                    wiki=args.wiki,
                    snapshot=args.snapshot,
                    stream_ordinal=args.stream_ordinal,
                    base_url=args.base_url,
                    tokenizer_path=args.tokenizer_path,
                    eos_token_id=args.eos_token_id,
                    context_length=args.context_length,
                    sequences_per_shard=args.sequences_per_shard,
                    split=split,
                )
            )
            summary = {
                "dataset": str(args.output.resolve()),
                "documents": manifest["extraction"]["documents_emitted"],
                "pages_seen": manifest["extraction"]["pages_seen"],
                "validated": True,
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        if args.command == "validate":
            report = validate_dataset(args.dataset)
            print(
                json.dumps(
                    {"checks": report.checks, "errors": report.errors, "ok": report.ok},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if report.ok else 1
        if args.command == "inspect":
            print(json.dumps(read_manifest(args.dataset), indent=2, sort_keys=True))
            return 0
    except (OSError, ValueError, WikiMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run())
