"""Portable identity belongs to files, and revisions belong to the locked writer."""

import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from meridian.lib.state.spawn.repository import Applied, Decline, write_state_locked
from meridian.lib.state.spawn_store import get_spawn, start_spawn


def test_identity_survives_mutation_and_transfer(tmp_path: Path) -> None:
    root = tmp_path / "original"
    spawn_id = start_spawn(
        root, chat_id="c1", model="model", agent="agent", harness="codex", prompt="hello"
    )
    original = get_spawn(root, spawn_id)
    assert original is not None
    assert isinstance(original.history_id, UUID)
    assert original.state_revision == 1
    result = write_state_locked(
        root / "spawns", str(spawn_id), lambda row: row.model_copy(update={"work_id": "work"})
    )
    assert isinstance(result, Applied)
    assert result.after.history_id == original.history_id
    assert result.after.state_revision == 2
    write_state_locked(root / "spawns", str(spawn_id), lambda _: Decline("unchanged"))
    assert get_spawn(root, spawn_id) == result.after

    destination = tmp_path / "relocated"
    shutil.copytree(root / "spawns", destination / "spawns")
    assert get_spawn(destination, spawn_id) == result.after

    for update in ({"history_id": uuid4()}, {"state_revision": 400}):
        with pytest.raises(ValueError, match="history identity or revision"):
            write_state_locked(
                root / "spawns",
                str(spawn_id),
                lambda row, update=update: row.model_copy(update=update),
            )
        assert get_spawn(root, spawn_id) == result.after


def test_unidentified_record_is_not_rewritten_by_reads(tmp_path: Path) -> None:
    spawn_id = start_spawn(
        tmp_path, chat_id="c1", model="model", agent="agent", harness="codex", prompt="hello"
    )
    state_path = tmp_path / "spawns" / str(spawn_id) / "state.json"
    raw = json.loads(state_path.read_text())
    raw.pop("history_id", None)
    raw.pop("state_revision", None)
    state_path.write_text(json.dumps(raw))
    before = state_path.read_bytes()
    first = get_spawn(tmp_path, spawn_id)
    assert first is not None
    assert first.history_id is None
    assert get_spawn(tmp_path, spawn_id) == first
    assert state_path.read_bytes() == before
    result = write_state_locked(tmp_path / "spawns", str(spawn_id), lambda row: row)
    assert isinstance(result, Applied)
    assert isinstance(result.after.history_id, UUID)
    assert result.after.state_revision == 1
    assert get_spawn(tmp_path, spawn_id) == result.after
