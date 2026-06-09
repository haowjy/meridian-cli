from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_tree import has_outstanding_descendant_work


def _start_row(
    runtime_root: Path,
    spawn_id: str,
    harness_id: HarnessId,
    parent_id: str | None,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=SpawnId(spawn_id),
        chat_id=spawn_id,
        parent_id=parent_id,
        model='test-model',
        agent='test-agent',
        harness=harness_id.value,
        prompt='hello',
        status='running',
    )


def test_outstanding_descendant_work_is_pure_read_for_grandchild(tmp_path: Path) -> None:
    _start_row(tmp_path, 'p1', HarnessId.CODEX, None)
    _start_row(tmp_path, 'p2', HarnessId.OPENCODE, 'p1')
    _start_row(tmp_path, 'p3', HarnessId.CODEX, 'p2')
    spawn_store.finalize_spawn(tmp_path, SpawnId('p2'), 'succeeded', 0, origin='runner')

    before = spawn_store.list_spawns(tmp_path)
    assert has_outstanding_descendant_work('p1', spawn_store.list_spawns(tmp_path)) is True
    after = spawn_store.list_spawns(tmp_path)

    assert before == after
    grandchild = spawn_store.get_spawn(tmp_path, 'p3')
    assert grandchild is not None
    assert grandchild.status == 'running'
