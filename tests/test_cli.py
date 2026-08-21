from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wikiml.cli import run
from wikiml.errors import SourceError


def test_validate_command_reports_missing_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    status = run(["validate", "/definitely/missing/wikiml-dataset"])

    output = json.loads(capsys.readouterr().out)
    assert status == 1
    assert output["ok"] is False


def test_probe_command_prints_machine_readable_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_probe(_config: object) -> dict[str, Any]:
        return {"extraction": {"documents_emitted": 2, "pages_seen": 5}}

    monkeypatch.setattr("wikiml.cli.run_probe", fake_probe)
    output_path = tmp_path / "dataset"

    status = run(["probe", "--output", str(output_path)])

    output = json.loads(capsys.readouterr().out)
    assert status == 0
    assert output["documents"] == 2
    assert output["validated"] is True


def test_inspect_command_prints_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")

    status = run(["inspect", str(tmp_path)])

    assert status == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1


def test_probe_command_reports_expected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_config: object) -> dict[str, Any]:
        raise SourceError("source unavailable")

    monkeypatch.setattr("wikiml.cli.run_probe", fail)

    status = run(["probe", "--output", str(tmp_path / "dataset")])

    assert status == 2
    assert "source unavailable" in capsys.readouterr().err


def test_build_command_reports_progress_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: dict[str, Any] = {}

    def fake_build(config: object, *, progress: Any) -> dict[str, Any]:
        observed["config"] = config
        progress(0, 2, 0)
        progress(1, 2, 0)
        progress(2, 2, 0)
        return {
            "source": {"stream_count": 2},
            "extraction": {"documents_emitted": 4, "pages_seen": 6},
        }

    monkeypatch.setattr("wikiml.cli.run_full_build", fake_build)
    output_path = tmp_path / "full"
    work_path = tmp_path / "work"
    tokenizer_path = tmp_path / "tokenizer.json"

    status = run(
        [
            "build",
            "--output",
            str(output_path),
            "--work-dir",
            str(work_path),
            "--wiki",
            "simplewiki",
            "--snapshot",
            "20260801",
            "--base-url",
            "https://example.test",
            "--workers",
            "1",
            "--tokenizer-json",
            str(tokenizer_path),
            "--eos-token-id",
            "1",
            "--context-length",
            "8",
            "--sequences-per-shard",
            "3",
            "--split-seed",
            "fixture-v1",
            "--train-bps",
            "9000",
            "--validation-bps",
            "500",
            "--test-bps",
            "500",
            "--near-duplicate-sample-size",
            "20",
            "--discard-work",
            "--fail-after-streams",
            "2",
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    progress_lines = [json.loads(line) for line in captured.err.splitlines()]
    config = observed["config"]
    assert status == 0
    assert output == {
        "dataset": str(output_path.resolve()),
        "documents": 4,
        "pages_seen": 6,
        "streams": 2,
        "validated": True,
    }
    assert progress_lines == [
        {
            "checkpoints_complete": 0,
            "checkpoints_reused": 0,
            "checkpoints_total": 2,
        },
        {
            "checkpoints_complete": 2,
            "checkpoints_reused": 0,
            "checkpoints_total": 2,
        },
    ]
    assert config.output_dir == output_path
    assert config.work_dir == work_path
    assert config.split.seed == "fixture-v1"
    assert config.keep_work_dir is False
    assert config.fail_after_streams == 2


@pytest.mark.parametrize(("passed", "status"), [(True, 0), (False, 1)])
def test_review_semantic_command_reports_gate_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passed: bool,
    status: int,
) -> None:
    observed: list[Path] = []

    def fake_review(dataset: Path, decisions: Path) -> dict[str, Any]:
        observed.extend((dataset, decisions))
        return {"passed": passed, "candidate_count": 30}

    monkeypatch.setattr("wikiml.cli.evaluate_semantic_review", fake_review)
    dataset = tmp_path / "dataset"
    decisions = tmp_path / "decisions.jsonl"

    result = run(["review-semantic", str(dataset), str(decisions)])

    assert result == status
    assert observed == [dataset, decisions]
    assert json.loads(capsys.readouterr().out)["candidate_count"] == 30
