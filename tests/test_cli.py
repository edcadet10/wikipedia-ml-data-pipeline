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
