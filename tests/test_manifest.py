from __future__ import annotations

from pathlib import Path

import pytest

from wikiml.manifest import read_manifest


def test_manifest_root_must_be_an_object(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="object"):
        read_manifest(tmp_path)
