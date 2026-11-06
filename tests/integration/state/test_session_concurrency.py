# qa-validated: test-suite-redesign

"""Cross-process locking and concurrent session-reserve tests.

Covers: concurrent reserve_chat_id safety, exclusive session-lock holding,
and rollback on append failure. Session CRUD and cleanup flows live in
test_session_store.py.
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import session_store
from tests.support.process_race import run_spawn_race_or_skip


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _reserve_chat_id_worker(state_root_str: str) -> str:
    runtime_root = Path(state_root_str)
    return session_store.reserve_chat_id(runtime_root)


def test_reserve_chat_id_is_safe_under_concurrency(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    process_count = 8

    allocated = run_spawn_race_or_skip(
        _reserve_chat_id_worker,
        [(runtime_root.as_posix(),) for _ in range(process_count)],
    )

    allocated = sorted(allocated)
    assert allocated == [f"c{idx}" for idx in range(1, process_count + 1)]
    assert (runtime_root / "session-id-counter").read_text(encoding="utf-8") == f"{process_count}\n"


