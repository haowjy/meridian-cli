"""Run allocation is serialized, generation-free, and independent of suffix holes."""

import os
from pathlib import Path

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn.runs import continue_target_run


@pytest.mark.parametrize('status,live,busy', [
    ('succeeded', False, False), ('running', False, False),
    ('running', True, True), ('queued', True, True),
])
def test_reserve_run_contract(tmp_path: Path, status: str, live: bool, busy: bool) -> None:
    first = spawn_store.start_spawn(
        tmp_path, spawn_id='p1', chat_id='c1', model='test', agent='', harness='codex', prompt='hi',
    )
    session_store.start_session(
        tmp_path, harness='codex', harness_session_id='native', model='test',
        chat_id='c1', spawn_id=first,
    )
    session_store.stop_session(tmp_path, 'c1')
    spawn_store.finalize_spawn(tmp_path, first, status='succeeded', exit_code=0, origin='runner')
    latest = spawn_store.start_spawn(
        tmp_path, spawn_id='p1.3', chat_id='c1', owner_chat_id='c9', model='test', agent='',
        harness='codex', prompt='hi', started_at='2000-01-01T00:00:00Z',
        runner_pid=os.getpid() if live else 999_999_999,
    )
    if status == 'succeeded':
        spawn_store.finalize_spawn(
            tmp_path, latest, status='succeeded', exit_code=0, origin='runner',
        )
    elif status == 'queued':
        # The publisher's PID keeps a queued reservation live beyond startup grace.
        from meridian.lib.state.spawn.repository import write_state_locked
        write_state_locked(tmp_path / 'spawns', latest,
                           lambda row: row.model_copy(update={'status': SpawnStatus.QUEUED}))
    reservation = SpawnReservation(
        chat_id='c1', owner_chat_id='c9', kind='primary', prompt='again',
        session_metadata=PrimarySessionMetadata(
            harness='codex', model='test', agent='', agent_path='', skills=(), skill_paths=(),
        ),
    )
    if busy:
        with pytest.raises(ValueError, match=r'busy.*p1\.3'):
            spawn_store.reserve_run(tmp_path, SpawnId('p1'), reservation)
        return
    new = spawn_store.reserve_run(tmp_path, SpawnId('p1'), reservation)
    assert new == 'p1.4'
    row = spawn_store.get_spawn(tmp_path, new)
    assert row is not None
    assert (row.session_instance_id, row.forked_from_history_id) == (None, None)
    assert row.runner_pid == os.getpid() and row.runner_created_at_epoch is not None
    assert (row.chat_id, row.owner_chat_id) == ('c1', 'c9')
    bound = spawn_store.get_spawn(tmp_path, parse_ref('p1', row_key=True))
    assert bound is not None
    assert continue_target_run([bound, row]) == bound
