from __future__ import annotations

import bz2
import hashlib
import json
import shutil
import stat
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from wikiml.errors import SourceError, ValidationError
from wikiml.full_pipeline import (
    FullBuildConfig,
    _parse_published_sha1,
    _validate_checkpoint,
    run_full_build,
)
from wikiml.full_validation import validate_full_dataset
from wikiml.models import SplitConfig, StreamRange
from wikiml.source import DownloadedBytes, DownloadedFile
from wikiml.storage import DOCUMENT_SCHEMA, PAGE_DECISION_SCHEMA, sha256_file
from wikiml.validation import validate_dataset


def _page(page_id: int, text: str, *, namespace: int = 0) -> bytes:
    return f"""
    <page>
      <title>Page {page_id}</title><ns>{namespace}</ns><id>{page_id}</id>
      <revision><id>{page_id + 1000}</id><timestamp>2026-08-01T00:00:00Z</timestamp>
      <text xml:space="preserve">{text}</text></revision>
    </page>
    """.encode()


class FakeFullClient:
    bodies: ClassVar[dict[str, bytes]] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> FakeFullClient:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass

    def content_length(self, url: str) -> int:
        return len(self.bodies[url])

    def download(self, url: str, *, max_bytes: int) -> DownloadedBytes:
        body = self.bodies[url]
        assert len(body) <= max_bytes
        return DownloadedBytes(body, '"fixture"', "fixture-date")

    def download_to_path(
        self,
        url: str,
        path: Path,
        *,
        max_bytes: int,
        expected_bytes: int | None = None,
    ) -> DownloadedFile:
        body = self.bodies[url]
        assert len(body) <= max_bytes
        assert expected_bytes is None or expected_bytes == len(body)
        path.write_bytes(body)
        return DownloadedFile(
            path,
            len(body),
            hashlib.sha1(body, usedforsecurity=False).hexdigest(),
            hashlib.sha256(body).hexdigest(),
            '"fixture"',
            "fixture-date",
        )


def _install_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "https://example.test/"
    prefix = "simplewiki-20260801-pages-articles-multistream"
    first = bz2.compress(
        b"".join(
            (
                _page(1, "Alpha is useful."),
                _page(2, "tiny"),
                _page(3, "discussion", namespace=1),
            )
        )
    )
    second = bz2.compress(
        b"".join(
            (
                _page(4, "word " * 200),
                _page(5, "word " * 1200),
                _page(6, "Café has text."),
                b"</mediawiki>",
            )
        )
    )
    dump = first + second
    index = bz2.compress(
        (
            "0:1:Page 1\n"
            "0:2:Page 2\n"
            "0:3:Page 3\n"
            f"{len(first)}:4:Page 4\n"
            f"{len(first)}:5:Page 5\n"
            f"{len(first)}:6:Page 6\n"
        ).encode()
    )
    dump_name = prefix + ".xml.bz2"
    index_name = prefix + "-index.txt.bz2"
    checksums = (
        f"{hashlib.sha1(dump, usedforsecurity=False).hexdigest()}  {dump_name}\n"
        f"{hashlib.sha1(index, usedforsecurity=False).hexdigest()}  {index_name}\n"
    ).encode()
    FakeFullClient.bodies = {
        base + dump_name: dump,
        base + index_name: index,
        base + "simplewiki-20260801-sha1sums.txt": checksums,
        base + "dumpstatus.json": json.dumps(
            {"jobs": {"articlesmultistreamdump": {"status": "done"}}}
        ).encode(),
    }
    monkeypatch.setattr("wikiml.full_pipeline.WikimediaClient", FakeFullClient)


def _config(
    tmp_path: Path,
    tokenizer_path: Path,
    *,
    name: str,
    workers: int = 1,
    fail_after_streams: int | None = None,
) -> FullBuildConfig:
    return FullBuildConfig(
        output_dir=tmp_path / name,
        work_dir=tmp_path / f"{name}-work",
        snapshot="20260801",
        base_url="https://example.test",
        workers=workers,
        tokenizer_path=tokenizer_path,
        eos_token_id=1,
        context_length=4,
        sequences_per_shard=2,
        near_duplicate_sample_size=5,
        split=SplitConfig(train_bps=10_000, validation_bps=0, test_bps=0),
        fail_after_streams=fail_after_streams,
    )


def test_full_build_is_resumable_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    interrupted = _config(tmp_path, tokenizer, name="resumed", fail_after_streams=1)

    with pytest.raises(ValidationError, match="injected termination"):
        run_full_build(interrupted)

    assert not interrupted.output_dir.exists()
    assert len(list((interrupted.resolved_work_dir / "checkpoints").glob("[0-9]*"))) == 1
    manifest = run_full_build(_config(tmp_path, tokenizer, name="resumed"))
    uninterrupted = run_full_build(_config(tmp_path, tokenizer, name="uninterrupted"))

    assert manifest["schema_version"] == 2
    assert manifest["source"]["stream_count"] == 2
    assert manifest["execution"]["checkpoints_reused"] == 1
    assert manifest["artifacts"] == uninterrupted["artifacts"]
    assert manifest["extraction"] == uninterrupted["extraction"]
    assert stat.S_IMODE(interrupted.output_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "uninterrupted").stat().st_mode) == 0o755
    assert validate_dataset(interrupted.output_dir).ok


def test_full_build_content_is_worker_order_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()

    serial = run_full_build(_config(tmp_path, tokenizer, name="serial", workers=1))
    parallel = run_full_build(_config(tmp_path, tokenizer, name="parallel", workers=2))

    assert (
        serial["artifacts"]["documents_content_sha256"]
        == parallel["artifacts"]["documents_content_sha256"]
    )
    assert serial["extraction"] == parallel["extraction"]
    assert serial["artifacts"] == parallel["artifacts"]


def test_full_build_accounts_for_indexed_page_with_missing_xml_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fixture(monkeypatch)
    base = "https://example.test/"
    prefix = "simplewiki-20260801-pages-articles-multistream"
    dump_name = prefix + ".xml.bz2"
    index_name = prefix + "-index.txt.bz2"
    dump = bz2.compress(
        b"<page><title>Broken</title><ns>0</ns>"
        b"<revision><id>1007</id><timestamp>2026-08-01T00:00:00Z</timestamp>"
        b"<text>content</text></revision></page></mediawiki>"
    )
    index = bz2.compress(b"0:7:Broken\n")
    checksums = (
        f"{hashlib.sha1(dump, usedforsecurity=False).hexdigest()}  {dump_name}\n"
        f"{hashlib.sha1(index, usedforsecurity=False).hexdigest()}  {index_name}\n"
    ).encode()
    FakeFullClient.bodies[base + dump_name] = dump
    FakeFullClient.bodies[base + index_name] = index
    FakeFullClient.bodies[base + "simplewiki-20260801-sha1sums.txt"] = checksums
    output = tmp_path / "missing-id"

    manifest = run_full_build(
        FullBuildConfig(
            output_dir=output,
            snapshot="20260801",
            base_url="https://example.test",
            workers=1,
            near_duplicate_sample_size=1,
        )
    )

    dropped = [
        json.loads(line) for line in (output / "dropped-pages.jsonl").read_text().splitlines()
    ]
    assert manifest["extraction"] == {
        "pages_seen": 1,
        "documents_emitted": 0,
        "pages_dropped": 1,
        "drop_counts": {
            "redirect": 0,
            "non_article_namespace": 0,
            "empty_text": 0,
            "insufficient_text": 0,
            "markup_residue": 0,
            "invalid_page": 1,
        },
    }
    assert dropped == [{"page_id": 7, "reason": "invalid_page", "title": "Broken"}]
    assert validate_dataset(output).ok


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"wiki": "simple"}, "wiki must look"),
        ({"snapshot": "latest"}, "dated YYYYMMDD"),
        ({"workers": 0}, "workers must be"),
        ({"workers": 65}, "workers must be"),
        ({"max_index_bytes": 0}, "download limits"),
        ({"tokenizer_path": Path("tokenizer.json")}, "supplied together"),
        ({"eos_token_id": 1}, "supplied together"),
        ({"context_length": 1}, "dimensions"),
        ({"sequences_per_shard": 0}, "dimensions"),
        ({"near_duplicate_sample_size": 0}, "sample_size"),
        ({"fail_after_streams": 0}, "must be positive"),
        ({"workers": 2, "fail_after_streams": 1}, "requires one worker"),
    ],
)
def test_full_build_config_rejects_unsafe_contracts(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {
        "output_dir": tmp_path / "dataset",
        "snapshot": "20260801",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        FullBuildConfig(**values)


def test_full_build_config_uses_sibling_work_directory(tmp_path: Path) -> None:
    config = FullBuildConfig(output_dir=tmp_path / "dataset", snapshot="20260801")

    assert config.resolved_work_dir == (tmp_path / ".dataset.work").resolve()


@pytest.mark.parametrize("relationship", ["same", "work_inside_output", "output_inside_work"])
def test_full_build_rejects_overlapping_work_and_output_directories(
    tmp_path: Path, relationship: str
) -> None:
    root = tmp_path / "root"
    if relationship == "same":
        output, work = root, root
    elif relationship == "work_inside_output":
        output, work = root, root / "work"
    else:
        output, work = root / "output", root

    with pytest.raises(ValidationError, match="must be disjoint"):
        run_full_build(FullBuildConfig(output_dir=output, work_dir=work, snapshot="20260801"))


def test_discarding_disjoint_work_preserves_published_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    config = _config(tmp_path, tokenizer, name="discarded")
    config = FullBuildConfig(
        output_dir=config.output_dir,
        work_dir=config.work_dir,
        snapshot=config.snapshot,
        base_url=config.base_url,
        workers=config.workers,
        tokenizer_path=config.tokenizer_path,
        eos_token_id=config.eos_token_id,
        context_length=config.context_length,
        sequences_per_shard=config.sequences_per_shard,
        near_duplicate_sample_size=config.near_duplicate_sample_size,
        split=config.split,
        keep_work_dir=False,
    )

    run_full_build(config)

    assert config.output_dir.is_dir()
    assert not config.resolved_work_dir.exists()
    assert validate_dataset(config.output_dir).ok


def test_cleanup_failure_after_publication_is_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    original_rmtree = shutil.rmtree
    config = _config(tmp_path, tokenizer, name="cleanup-warning")
    config = FullBuildConfig(
        output_dir=config.output_dir,
        work_dir=config.work_dir,
        snapshot=config.snapshot,
        base_url=config.base_url,
        workers=config.workers,
        tokenizer_path=config.tokenizer_path,
        eos_token_id=config.eos_token_id,
        context_length=config.context_length,
        sequences_per_shard=config.sequences_per_shard,
        near_duplicate_sample_size=config.near_duplicate_sample_size,
        split=config.split,
        keep_work_dir=False,
    )

    def fail_only_for_work(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path).resolve() == config.resolved_work_dir:
            raise PermissionError("fixture denies checkpoint cleanup")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("wikiml.full_pipeline.shutil.rmtree", fail_only_for_work)

    with pytest.warns(RuntimeWarning, match="dataset was published"):
        manifest = run_full_build(config)

    assert manifest["schema_version"] == 2
    assert config.output_dir.is_dir()
    assert validate_dataset(config.output_dir).ok


def test_published_checksum_parser_requires_one_utf8_match() -> None:
    with pytest.raises(SourceError, match="not UTF-8"):
        _parse_published_sha1(b"\xff", "dump.xml.bz2")
    with pytest.raises(SourceError, match="uniquely name"):
        _parse_published_sha1(b"0" * 40 + b"  other.xml.bz2\n", "dump.xml.bz2")
    duplicate = (b"0" * 40 + b"  dump.xml.bz2\n") * 2
    with pytest.raises(SourceError, match="uniquely name"):
        _parse_published_sha1(duplicate, "dump.xml.bz2")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda bodies: bodies.__setitem__("status", b"{"), "unexpected shape"),
        (
            lambda bodies: bodies.__setitem__(
                "status",
                json.dumps({"jobs": {"articlesmultistreamdump": {"status": "waiting"}}}).encode(),
            ),
            "not marked done",
        ),
        (lambda bodies: bodies.__setitem__("index", b"corrupt-index"), "published SHA-1"),
        (
            lambda bodies: bodies.__setitem__(
                "checksums", bodies["checksums"].replace(b"4", b"0", 1)
            ),
            "published SHA-1",
        ),
    ],
)
def test_full_build_rejects_untrusted_source_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, bytes]], None],
    message: str,
) -> None:
    _install_fixture(monkeypatch)
    urls = {
        "status": "https://example.test/dumpstatus.json",
        "index": (
            "https://example.test/simplewiki-20260801-pages-articles-multistream-index.txt.bz2"
        ),
        "checksums": "https://example.test/simplewiki-20260801-sha1sums.txt",
    }
    aliases = {name: FakeFullClient.bodies[url] for name, url in urls.items()}
    mutate(aliases)
    for name, url in urls.items():
        FakeFullClient.bodies[url] = aliases[name]

    with pytest.raises(SourceError, match=message):
        run_full_build(
            FullBuildConfig(
                output_dir=tmp_path / "dataset",
                snapshot="20260801",
                base_url="https://example.test",
                max_dump_bytes=1 if message == "dump limit" else 1024 * 1024,
            )
        )


def test_full_build_rejects_dump_over_configured_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fixture(monkeypatch)

    with pytest.raises(SourceError, match="limit is 1"):
        run_full_build(
            FullBuildConfig(
                output_dir=tmp_path / "dataset",
                snapshot="20260801",
                base_url="https://example.test",
                max_dump_bytes=1,
            )
        )


def test_full_build_rejects_corrupt_cache_and_work_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    interrupted = _config(tmp_path, tokenizer, name="cache", fail_after_streams=1)
    with pytest.raises(ValidationError, match="injected termination"):
        run_full_build(interrupted)
    dump_path = next((interrupted.resolved_work_dir / "source").glob("*.xml.bz2"))
    original_dump = dump_path.read_bytes()

    dump_path.write_bytes(original_dump + b"x")
    with pytest.raises(SourceError, match="byte count"):
        run_full_build(_config(tmp_path, tokenizer, name="cache"))
    dump_path.write_bytes(b"x" + original_dump[1:])
    with pytest.raises(SourceError, match="cached dump does not match"):
        run_full_build(_config(tmp_path, tokenizer, name="cache"))
    dump_path.write_bytes(original_dump)

    state_path = interrupted.resolved_work_dir / "run-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["identity_sha256"] = "0" * 64
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValidationError, match="different build identity"):
        run_full_build(_config(tmp_path, tokenizer, name="cache"))


def test_checkpoint_validation_rejects_corrupt_reuse_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    config = _config(tmp_path, tokenizer, name="checkpoint", fail_after_streams=1)
    with pytest.raises(ValidationError, match="injected termination"):
        run_full_build(config)

    checkpoint_dir = config.resolved_work_dir / "checkpoints" / "000000"
    checkpoint_path = checkpoint_dir / "checkpoint.json"
    original = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    stream = StreamRange(**original["stream"])
    identity = str(original["identity_sha256"])

    cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
        (lambda value: value.__setitem__("schema_version", 99), "schema"),
        (lambda value: value.__setitem__("identity_sha256", "bad"), "identity"),
        (lambda value: value["stream"].__setitem__("ordinal", 9), "range"),
        (lambda value: value["artifacts"]["documents"].__setitem__("bytes", 0), "byte"),
        (lambda value: value["artifacts"]["documents"].__setitem__("sha256", "0" * 64), "hash"),
        (lambda value: value["artifacts"]["documents"].__setitem__("records", 99), "count"),
        (
            lambda value: value["artifacts"].__setitem__("documents_content_sha256", "0" * 64),
            "content hash",
        ),
        (lambda value: value["artifacts"]["dropped_pages"].__setitem__("records", 99), "count"),
        (lambda value: value["extraction"].__setitem__("pages_seen", 99), "accounting"),
        (lambda value: value["segment"].__setitem__("bytes", 0), "segment"),
    ]
    for mutate, message in cases:
        candidate = deepcopy(original)
        mutate(candidate)
        checkpoint_path.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises(ValidationError, match=message):
            _validate_checkpoint(
                checkpoint_dir,
                dump_path=next((config.resolved_work_dir / "source").glob("*.xml.bz2")),
                stream=stream,
                expected_page_ids=(1, 2, 3),
                split=config.split,
                identity_sha256=identity,
            )
    checkpoint_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid checkpoint"):
        _validate_checkpoint(
            checkpoint_dir,
            dump_path=next((config.resolved_work_dir / "source").glob("*.xml.bz2")),
            stream=stream,
            expected_page_ids=(1, 2, 3),
            split=config.split,
            identity_sha256=identity,
        )


def _set_path(root: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = root
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value


def test_full_validation_rejects_manifest_contract_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    config = _config(tmp_path, tokenizer, name="validation")
    run_full_build(config)
    manifest = json.loads((config.output_dir / "manifest.json").read_text(encoding="utf-8"))

    cases = [
        (("project", "scope"), "wrong", "scope mismatch"),
        (("project", "pipeline_contract_version"), 99, "contract version"),
        (("source", "snapshot"), "latest", "not a dated snapshot"),
        (("source", "dump_status"), "waiting", "not marked done"),
        (("source", "dump_sha1"), "0" * 40, "dump SHA-1"),
        (("source", "index_sha1"), "0" * 40, "index SHA-1"),
        (("source", "index_sha256"), "0" * 64, "bundled source index SHA-256"),
        (("source", "indexed_page_count"), 99, "indexed page count"),
        (("source", "indexed_page_ids_sha256"), "0" * 64, "page-ID hash"),
        (("source", "stream_count"), 99, "ledger count"),
        (("source", "dump_bytes"), 10_000, "end of the dump"),
        (("artifacts", "streams", "records"), 99, "artifact record count"),
        (("artifacts", "source_index", "records"), 99, "source-index artifact"),
        (("artifacts", "page_decisions", "records"), 99, "page-decision artifact"),
        (("split", "strategy"), "random", "split strategy"),
        (("artifacts", "documents", "records"), 99, "document artifact record"),
        (("extraction", "documents_emitted"), 99, "extraction document count"),
        (("artifacts", "documents_content_sha256"), "0" * 64, "content hash"),
        (("artifacts", "attribution_content_sha256"), "0" * 64, "attribution content"),
        (("artifacts", "dropped_pages", "records"), 99, "dropped-page artifact"),
        (("extraction", "pages_dropped"), 99, "extraction dropped-page"),
        (("extraction", "pages_seen"), 99, "not every full-dump"),
        (("artifacts", "semantic_review_candidates", "records"), 99, "candidate count"),
        (("artifacts", "tokenization", "dtype"), "float32", "token dtype"),
        (("artifacts", "tokenization", "tokenizer_path"), "missing.json", "tokenizer artifact"),
        (("artifacts", "tokenization", "tokenizer_bytes"), 0, "tokenizer byte count"),
        (("artifacts", "tokenization", "tokenizer_sha256"), "0" * 64, "tokenizer SHA-256"),
        (("artifacts", "tokenization", "vocab_size"), 1, "out-of-vocabulary"),
        (("artifacts", "training_smoke", "loss_after"), 99.0, "did not reduce"),
        (("artifacts", "training_smoke", "shard"), "missing.bin", "missing shard"),
    ]
    for path, value, message in cases:
        candidate = deepcopy(manifest)
        _set_path(candidate, path, value)
        report = validate_full_dataset(config.output_dir, candidate)
        assert any(message in error for error in report.errors), (path, report.errors)


def test_full_validation_rejects_artifact_and_ledger_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_factory: Callable[[], Path],
) -> None:
    _install_fixture(monkeypatch)
    tokenizer = tokenizer_factory()
    config = _config(tmp_path, tokenizer, name="artifact-validation")
    run_full_build(config)
    manifest = json.loads((config.output_dir / "manifest.json").read_text(encoding="utf-8"))

    for key, value, message in (
        ("bytes", 0, "byte count"),
        ("sha256", "0" * 64, "SHA-256"),
        ("path", "missing.parquet", "missing documents"),
    ):
        candidate = deepcopy(manifest)
        candidate["artifacts"]["documents"][key] = value
        report = validate_full_dataset(config.output_dir, candidate)
        assert any(message in error for error in report.errors)

    documents_path = config.output_dir / manifest["artifacts"]["documents"]["path"]
    original_documents = documents_path.read_bytes()
    document_rows = pq.read_table(documents_path).to_pylist()
    document_rows[0]["text"] += " < ReFNaMe='residual' />"
    document_rows[0]["text_sha256"] = hashlib.sha256(document_rows[0]["text"].encode()).hexdigest()
    pq.write_table(pa.Table.from_pylist(document_rows, schema=DOCUMENT_SCHEMA), documents_path)
    candidate = deepcopy(manifest)
    documents_meta = candidate["artifacts"]["documents"]
    documents_meta["bytes"] = documents_path.stat().st_size
    documents_meta["sha256"] = sha256_file(documents_path)
    report = validate_full_dataset(config.output_dir, candidate)
    assert any("structural markup" in error for error in report.errors)
    documents_path.write_bytes(original_documents)

    document_rows = pq.read_table(documents_path).to_pylist()
    document_rows[0]["text"] += ' Specs: | style="text-align:center" | 42 kg.'
    document_rows[0]["text_sha256"] = hashlib.sha256(document_rows[0]["text"].encode()).hexdigest()
    pq.write_table(pa.Table.from_pylist(document_rows, schema=DOCUMENT_SCHEMA), documents_path)
    candidate = deepcopy(manifest)
    documents_meta = candidate["artifacts"]["documents"]
    documents_meta["bytes"] = documents_path.stat().st_size
    documents_meta["sha256"] = sha256_file(documents_path)
    report = validate_full_dataset(config.output_dir, candidate)
    assert any("1 documents retain structural markup" in error for error in report.errors)
    documents_path.write_bytes(original_documents)

    document_rows = pq.read_table(documents_path).to_pylist()
    document_rows[0]["text"] += " Alternate HTML comment close --!> survives."
    document_rows[0]["text_sha256"] = hashlib.sha256(document_rows[0]["text"].encode()).hexdigest()
    pq.write_table(pa.Table.from_pylist(document_rows, schema=DOCUMENT_SCHEMA), documents_path)
    candidate = deepcopy(manifest)
    documents_meta = candidate["artifacts"]["documents"]
    documents_meta["bytes"] = documents_path.stat().st_size
    documents_meta["sha256"] = sha256_file(documents_path)
    report = validate_full_dataset(config.output_dir, candidate)
    assert any("1 documents retain structural markup" in error for error in report.errors)
    documents_path.write_bytes(original_documents)

    streams_path = config.output_dir / manifest["artifacts"]["streams"]["path"]
    original_streams = streams_path.read_bytes()
    records = [json.loads(line) for line in original_streams.decode().splitlines()]
    records[0]["stream"]["ordinal"] = 9
    records[1]["stream"]["start"] += 1
    records[0]["segment"]["bytes"] += 1
    records[0]["segment"]["sha256"] = "bad"
    records[0]["extraction"]["pages_seen"] += 1
    streams_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    candidate = deepcopy(manifest)
    stream_meta = candidate["artifacts"]["streams"]
    stream_meta["bytes"] = streams_path.stat().st_size
    stream_meta["sha256"] = sha256_file(streams_path)
    report = validate_full_dataset(config.output_dir, candidate)
    assert any("ordinals" in error for error in report.errors)
    assert any("contiguous" in error for error in report.errors)
    assert any("byte accounting" in error for error in report.errors)
    assert any("invalid source hash" in error for error in report.errors)
    assert any("page accounting" in error for error in report.errors)
    streams_path.write_bytes(original_streams)

    token_meta = deepcopy(manifest)
    token_meta["artifacts"]["tokenization"]["shards"] = "not-an-array"
    report = validate_full_dataset(config.output_dir, token_meta)
    assert any("token shards must be an array" in error for error in report.errors)

    token_meta = deepcopy(manifest)
    shard = token_meta["artifacts"]["tokenization"]["shards"][0]
    shard["sequences"] += 1
    shard["sha256"] = "0" * 64
    report = validate_full_dataset(config.output_dir, token_meta)
    assert any("size mismatch" in error for error in report.errors)
    assert any("SHA-256 mismatch" in error for error in report.errors)

    token_meta = deepcopy(manifest)
    token_meta["artifacts"]["tokenization"]["shards"][0]["path"] = "missing.bin"
    report = validate_full_dataset(config.output_dir, token_meta)
    assert any("missing token shard" in error for error in report.errors)

    audit_path = config.output_dir / manifest["artifacts"]["corpus_audit"]["path"]
    original_audit = audit_path.read_bytes()
    for section, field, value, message in (
        ("near_duplicates", "observed_sample_size", 99, "near-duplicate sample audit"),
        ("semantic_review", "candidate_count", 99, "semantic review plan"),
    ):
        audit = json.loads(original_audit)
        audit[section][field] = value
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        candidate = deepcopy(manifest)
        audit_meta = candidate["artifacts"]["corpus_audit"]
        audit_meta["bytes"] = audit_path.stat().st_size
        audit_meta["sha256"] = sha256_file(audit_path)
        report = validate_full_dataset(config.output_dir, candidate)
        assert any(message in error for error in report.errors)
        audit_path.write_bytes(original_audit)

    review_path = config.output_dir / manifest["artifacts"]["semantic_review_candidates"]["path"]
    original_review = review_path.read_bytes()
    review_lines = original_review.decode().splitlines()
    review_path.write_text("\n".join(review_lines[1:]) + "\n", encoding="utf-8")
    candidate = deepcopy(manifest)
    review_meta = candidate["artifacts"]["semantic_review_candidates"]
    review_meta["bytes"] = review_path.stat().st_size
    review_meta["sha256"] = sha256_file(review_path)
    review_meta["records"] = len(review_lines) - 1
    report = validate_full_dataset(config.output_dir, candidate)
    assert any("pre-registered sample" in error for error in report.errors)
    review_path.write_bytes(original_review)

    decisions_path = config.output_dir / manifest["artifacts"]["page_decisions"]["path"]
    original_decisions = decisions_path.read_bytes()
    decision_rows = pq.read_table(decisions_path).to_pylist()
    mutations: list[tuple[list[dict[str, Any]], str]] = []
    extra = deepcopy(decision_rows)
    extra.append(deepcopy(extra[-1]))
    mutations.append((extra, "absent from the source index"))
    invalid = deepcopy(decision_rows)
    invalid[0]["decision"] = "invalid"
    mutations.append((invalid, "invalid page decision"))
    mutations.append((deepcopy(decision_rows[:-1]), "omits pages"))
    for rows, message in mutations:
        pq.write_table(pa.Table.from_pylist(rows, schema=PAGE_DECISION_SCHEMA), decisions_path)
        candidate = deepcopy(manifest)
        decision_meta = candidate["artifacts"]["page_decisions"]
        decision_meta["bytes"] = decisions_path.stat().st_size
        decision_meta["sha256"] = sha256_file(decisions_path)
        decision_meta["records"] = len(rows)
        report = validate_full_dataset(config.output_dir, candidate)
        assert any(message in error for error in report.errors)
        decisions_path.write_bytes(original_decisions)
