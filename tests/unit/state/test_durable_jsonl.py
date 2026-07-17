from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.state.atomic import append_durable_jsonl_line

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_append_durable_jsonl_line_succeeds_when_repair_replace_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    torn = b'{"id":1,"kind":"start"}\n{"id":2,"kind":"to'
    path.write_bytes(torn)
    new_line = '{"id":3,"kind":"new"}\n'

    import meridian.lib.platform.atomic as platform_atomic

    def _deny_replace(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        raise PermissionError("open file blocks replace on Windows")

    monkeypatch.setattr(platform_atomic.os, "replace", _deny_replace)

    append_durable_jsonl_line(path, new_line)

    assert path.read_bytes() == torn + new_line.encode("utf-8")
