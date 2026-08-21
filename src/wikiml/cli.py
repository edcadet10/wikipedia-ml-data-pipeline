"""Command-line interface with machine-readable output and explicit failure codes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from wikiml import __version__
from wikiml.errors import WikiMLError
from wikiml.full_pipeline import FullBuildConfig, run_full_build
from wikiml.manifest import read_manifest
from wikiml.models import SplitConfig
from wikiml.pipeline import ProbeConfig, run_probe
from wikiml.review import evaluate_semantic_review
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

    build = subparsers.add_parser(
        "build", help="process a complete dated dump with resumable checkpoints"
    )
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--work-dir", type=Path)
    build.add_argument("--wiki", default="simplewiki")
    build.add_argument("--snapshot", required=True)
    build.add_argument("--base-url")
    build.add_argument("--workers", type=int, default=4)
    build.add_argument("--tokenizer-json", type=Path, dest="tokenizer_path")
    build.add_argument("--eos-token-id", type=int)
    build.add_argument("--context-length", type=int, default=1024)
    build.add_argument("--sequences-per-shard", type=int, default=4096)
    build.add_argument("--split-seed", default="wikiml-v1")
    build.add_argument("--train-bps", type=int, default=9800)
    build.add_argument("--validation-bps", type=int, default=100)
    build.add_argument("--test-bps", type=int, default=100)
    build.add_argument("--near-duplicate-sample-size", type=int, default=5_000)
    build.add_argument("--discard-work", action="store_true")
    build.add_argument(
        "--fail-after-streams",
        type=int,
        help="inject a deterministic interruption (requires --workers 1)",
    )

    validate = subparsers.add_parser("validate", help="verify every declared artifact")
    validate.add_argument("dataset", type=Path)

    inspect = subparsers.add_parser("inspect", help="print the complete dataset manifest")
    inspect.add_argument("dataset", type=Path)
    review = subparsers.add_parser(
        "review-semantic", help="verify human labels for the pre-registered review packet"
    )
    review.add_argument("dataset", type=Path)
    review.add_argument("decisions", type=Path)
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
        if args.command == "build":
            split = SplitConfig(
                train_bps=args.train_bps,
                validation_bps=args.validation_bps,
                test_bps=args.test_bps,
                seed=args.split_seed,
            )
            last_reported = -100

            def report_progress(completed: int, total: int, reused: int) -> None:
                nonlocal last_reported
                if completed == total or completed == 0 or completed - last_reported >= 100:
                    print(
                        json.dumps(
                            {
                                "checkpoints_complete": completed,
                                "checkpoints_reused": reused,
                                "checkpoints_total": total,
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    last_reported = completed

            manifest = run_full_build(
                FullBuildConfig(
                    output_dir=args.output,
                    work_dir=args.work_dir,
                    wiki=args.wiki,
                    snapshot=args.snapshot,
                    base_url=args.base_url,
                    workers=args.workers,
                    tokenizer_path=args.tokenizer_path,
                    eos_token_id=args.eos_token_id,
                    context_length=args.context_length,
                    sequences_per_shard=args.sequences_per_shard,
                    split=split,
                    near_duplicate_sample_size=args.near_duplicate_sample_size,
                    keep_work_dir=not args.discard_work,
                    fail_after_streams=args.fail_after_streams,
                ),
                progress=report_progress,
            )
            print(
                json.dumps(
                    {
                        "dataset": str(args.output.resolve()),
                        "documents": manifest["extraction"]["documents_emitted"],
                        "pages_seen": manifest["extraction"]["pages_seen"],
                        "streams": manifest["source"]["stream_count"],
                        "validated": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
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
        if args.command == "review-semantic":
            result = evaluate_semantic_review(args.dataset, args.decisions)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["passed"] else 1
    except (OSError, ValueError, WikiMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run())
