"""``spawn list --global`` names a project once, whichever path reached it."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.ops.spawn.api import SpawnListInput, spawn_list_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_lock import acquire, lock_path
from meridian.lib.state.machine_ids import allocate
from meridian.lib.state.user_paths import ensure_project_lifetime_lock

if TYPE_CHECKING:
    import pytest


def test_one_running_session_is_listed_once_when_the_home_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS: /tmp is /private/tmp. The lock scan walks the home as given; a claim's link holds
    # the resolved path, so the same root arrives by two spellings.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("MERIDIAN_HOME", str(link))
    root = link / "projects" / "proj"
    # The runtime root as an allocator that resolved the path spells it.
    spawn_id = allocate("p", real / "projects" / "proj")
    spawn_store.start_spawn(
        root,
        spawn_id=spawn_id,
        chat_id=allocate("c", root),
        model="m",
        agent="a",
        harness="claude",
        kind="primary",
        prompt="x",
        worker_pid=os.getpid(),
    )
    lock_path(root, spawn_id).touch()
    held = acquire(root, spawn_id)
    life = os.open(ensure_project_lifetime_lock(root), os.O_RDWR)
    fcntl.flock(life, fcntl.LOCK_SH)
    try:
        listed = spawn_list_sync(SpawnListInput(all_projects=True, statuses=("running",)))
    finally:
        os.close(life)
        held.close()

    assert [entry.spawn_id for entry in listed.spawns] == [spawn_id]
