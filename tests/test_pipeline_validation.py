from __future__ import annotations

import bz2
import json
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from wikiml.errors import SourceError, ValidationError
from wikiml.models import SplitConfig, StreamRange
from wikiml.pipeline import ProbeConfig, run_probe
from wikiml.source import DownloadedBytes
from wikiml.storage import sha256_file
from wikiml.validation import validate_dataset


class FakeWikimediaClient:
    segment = b""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> FakeWikimediaClient:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass

    def content_length(self, _url: str) -> int:
        return 10 + len(self.segment)

    def download(self, _url: str, *, max_bytes: int) -> DownloadedBytes:
        index = bz2.compress(b"10:1:Alpha\n10:2:Redirect\n10:3:Talk\n10:4:Empty\n10:5:Unicode\n")
        assert len(index) < max_bytes
        return DownloadedBytes(index, '"index"', "Thu, 20 Aug 2026 12:00:00 GMT")

    def download_range(
        self, _url: str, stream_range: StreamRange, *, max_bytes: int
    ) -> DownloadedBytes:
        assert stream_range.start == 10
        assert len(self.segment) < max_bytes
        return DownloadedBytes(self.segment, '"dump"', "Thu, 20 Aug 2026 12:00:00 GMT")


def _build_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_path: Path,
) -> Path:
    FakeWikimediaClient.segment = segment_bz2
    monkeypatch.setattr("wikiml.pipeline.WikimediaClient", FakeWikimediaClient)
    output = tmp_path / "dataset"
    run_probe(
        ProbeConfig(
            output_dir=output,
            tokenizer_path=tokenizer_path,
            eos_token_id=1,
            context_length=4,
            sequences_per_shard=2,
            split=SplitConfig(train_bps=10_000, validation_bps=0, test_bps=0),
        )
    )
    return output


def _rewrite_manifest(output: Path, mutation: Callable[[dict[str, Any]], None]) -> None:
    path = output / "manifest.json"
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    mutation(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_probe_builds_then_validates_atomic_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    FakeWikimediaClient.segment = segment_bz2
    monkeypatch.setattr("wikiml.pipeline.WikimediaClient", FakeWikimediaClient)
    output = tmp_path / "dataset"
    config = ProbeConfig(
        output_dir=output,
        tokenizer_path=tokenizer_factory(),
        eos_token_id=1,
        context_length=4,
        sequences_per_shard=2,
        split=SplitConfig(train_bps=10_000, validation_bps=0, test_bps=0),
    )

    manifest = run_probe(config)
    report = validate_dataset(output)

    assert manifest["project"]["scope"] == "one_multistream_segment"
    assert manifest["extraction"]["pages_seen"] == 5
    assert report.ok, report.errors
    assert (output / "documents.parquet").is_file()
    assert not list(tmp_path.glob(".dataset.staging-*"))

    with pytest.raises(ValidationError, match="already exists"):
        run_probe(config)


def test_validation_detects_token_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    FakeWikimediaClient.segment = segment_bz2
    monkeypatch.setattr("wikiml.pipeline.WikimediaClient", FakeWikimediaClient)
    output = tmp_path / "dataset"
    run_probe(
        ProbeConfig(
            output_dir=output,
            tokenizer_path=tokenizer_factory(),
            eos_token_id=1,
            context_length=4,
            sequences_per_shard=2,
            split=SplitConfig(train_bps=10_000, validation_bps=0, test_bps=0),
        )
    )
    shard = next((output / "tokens").glob("*.bin"))
    shard.write_bytes(shard.read_bytes() + b"\x00")

    report = validate_dataset(output)

    assert not report.ok
    assert any("size mismatch" in error for error in report.errors)


def test_probe_can_emit_document_only_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
) -> None:
    FakeWikimediaClient.segment = segment_bz2
    monkeypatch.setattr("wikiml.pipeline.WikimediaClient", FakeWikimediaClient)
    output = tmp_path / "documents-only"

    manifest = run_probe(ProbeConfig(output_dir=output))

    assert manifest["artifacts"]["tokenization"] is None
    assert validate_dataset(output).ok


def test_probe_cleans_staging_after_invalid_stream_ordinal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, segment_bz2: bytes
) -> None:
    FakeWikimediaClient.segment = segment_bz2
    monkeypatch.setattr("wikiml.pipeline.WikimediaClient", FakeWikimediaClient)

    with pytest.raises(SourceError, match="outside"):
        run_probe(ProbeConfig(output_dir=tmp_path / "failed", stream_ordinal=1))

    assert not list(tmp_path.glob(".failed.staging-*"))


def test_validation_rejects_unsupported_manifest_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    output = _build_dataset(tmp_path, monkeypatch, segment_bz2, tokenizer_factory())
    _rewrite_manifest(output, lambda manifest: manifest.__setitem__("schema_version", 999))

    report = validate_dataset(output)

    assert report.errors == ("unsupported manifest schema_version",)


def test_validation_rejects_unsafe_artifact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    output = _build_dataset(tmp_path, monkeypatch, segment_bz2, tokenizer_factory())

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["artifacts"]["documents"]["path"] = "../escape.parquet"

    _rewrite_manifest(output, mutate)
    report = validate_dataset(output)

    assert not report.ok
    assert any("escapes" in error for error in report.errors)


def test_validation_rejects_unaccounted_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    output = _build_dataset(tmp_path, monkeypatch, segment_bz2, tokenizer_factory())

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["extraction"]["pages_seen"] += 1

    _rewrite_manifest(output, mutate)
    report = validate_dataset(output)

    assert any("accounted" in error for error in report.errors)


def test_validation_rejects_out_of_vocabulary_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    output = _build_dataset(tmp_path, monkeypatch, segment_bz2, tokenizer_factory())
    manifest_path = output / "manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    shard_meta = manifest["artifacts"]["tokenization"]["shards"][0]
    shard = output / shard_meta["path"]
    content = bytearray(shard.read_bytes())
    content[0:2] = (65_535).to_bytes(2, "little")
    shard.write_bytes(content)
    shard_meta["sha256"] = sha256_file(shard)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    report = validate_dataset(output)

    assert any("out-of-vocabulary" in error for error in report.errors)


def test_validation_rejects_unknown_token_dtype(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_bz2: bytes,
    tokenizer_factory: Callable[[], Path],
) -> None:
    output = _build_dataset(tmp_path, monkeypatch, segment_bz2, tokenizer_factory())

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["artifacts"]["tokenization"]["dtype"] = "float32"

    _rewrite_manifest(output, mutate)
    report = validate_dataset(output)

    assert any("unsupported token dtype" in error for error in report.errors)


@pytest.mark.parametrize(
    "factory",
    [
        lambda path: ProbeConfig(output_dir=path, wiki="simple"),
        lambda path: ProbeConfig(output_dir=path, snapshot="../../bad"),
        lambda path: ProbeConfig(output_dir=path, stream_ordinal=-1),
        lambda path: ProbeConfig(output_dir=path, tokenizer_path=Path("tokenizer.json")),
    ],
)
def test_probe_config_rejects_unsafe_or_incomplete_input(
    tmp_path: Path, factory: Callable[[Path], ProbeConfig]
) -> None:
    with pytest.raises(ValueError):
        factory(tmp_path / "output")
