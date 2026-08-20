from __future__ import annotations

import bz2
from collections.abc import Callable
from pathlib import Path

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace


def _page(
    title: str,
    namespace: int,
    page_id: int,
    revision_id: int,
    text: str,
    *,
    redirect: bool = False,
) -> str:
    redirect_xml = '<redirect title="Target" />' if redirect else ""
    return f"""
    <page>
      <title>{title}</title>
      <ns>{namespace}</ns>
      <id>{page_id}</id>
      {redirect_xml}
      <revision>
        <id>{revision_id}</id>
        <timestamp>2026-08-20T12:00:00Z</timestamp>
        <text xml:space="preserve">{text}</text>
      </revision>
    </page>
    """


@pytest.fixture
def segment_bz2() -> bytes:
    xml = "".join(
        [
            _page("Alpha", 0, 1, 101, "'''Alpha''' is [[Useful|useful]].\n\nSecond line."),
            _page("Redirect", 0, 2, 102, "#REDIRECT [[Alpha]]", redirect=True),
            _page("Talk:Alpha", 1, 3, 103, "Discussion"),
            _page("Empty", 0, 4, 104, "{{OnlyTemplate}}"),
            _page("Unicode", 0, 5, 105, "Caf&#233; has text."),
        ]
    )
    return bz2.compress(xml.encode())


@pytest.fixture
def tokenizer_factory(tmp_path: Path) -> Callable[[], Path]:
    def create() -> Path:
        tokenizer = Tokenizer(
            WordLevel(
                {
                    "[UNK]": 0,
                    "[EOS]": 1,
                    "Alpha": 2,
                    "is": 3,
                    "useful": 4,
                    ".": 5,
                    "Second": 6,
                    "line": 7,
                    "Café": 8,
                    "has": 9,
                    "text": 10,
                },
                unk_token="[UNK]",
            )
        )
        tokenizer.pre_tokenizer = Whitespace()
        path = tmp_path / "tokenizer.json"
        tokenizer.save(str(path))
        return path

    return create
