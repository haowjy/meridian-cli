"""A newer build's spawn row still names the chat an older build acts on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.resolved_context import LocalFilesystemBackend
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.session_links import execution_of


def _write_v4_row(runtime_root: Path, *, current_chat_id: str | None) -> None:
    row = runtime_root / "spawns" / "p1" / "state.json"
    row.parent.mkdir(parents=True)
    row.write_text(
        json.dumps({"v": 5, "id": "p1", "chat_id": "c1", "current_chat_id": current_chat_id}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(("current", "expected"), [(None, "c1"), ("c5", "c5")])
def test_execution_chat_follows_current_else_entry_chat(
    tmp_path: Path, current: str | None, expected: str
) -> None:
    _write_v4_row(tmp_path, current_chat_id=current)

    assert LocalFilesystemBackend().get_execution_chat(tmp_path, SpawnId("p1")) == expected


@pytest.mark.parametrize(("current", "chats"), [(None, ("c0", "c1")), ("c5", ("c0", "c1", "c5"))])
def test_execution_of_links_the_entry_chat(
    tmp_path: Path, current: str | None, chats: tuple[str, ...]
) -> None:
    _write_v4_row(tmp_path, current_chat_id=current)

    execution = execution_of(tmp_path, chat_id="c0", spawn_id="p1")

    assert execution.entry_chat == "c1"
    assert execution.chats == chats
