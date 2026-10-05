"""Session-scoped discovery and bounded recovery from real history-source contention."""

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Connection

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.state.history_index as history_index
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.sink import NullSink
from meridian.lib.core.types import ChatId, HarnessId
from meridian.lib.ops.session_archive import archive_history
from meridian.lib.ops.spawn.models import SpawnWaitInput
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import spawn_store
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn.model import SpawnRecord
from tests.support.resident_drain import start_row


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    root.mkdir(parents=True, exist_ok=True)
    return project, root


def _spawn(root: Path, name: str, *, owner: str = "c-owner", parent: str | None = None) -> str:
    return str(
        spawn_store.start_spawn(
            root,
            spawn_id=name,
            chat_id=owner,
            owner_chat_id=owner,
            parent_id=parent,
            model="test",
            agent="coder",
            harness="pi",
            prompt="wait discovery",
        )
    )


@contextmanager
def _busy_sessions(root: Path) -> Iterator[subprocess.Popen[str]]:
    """Keep a real source lock busy until the caller closes the child's stdin."""
    source = HistorySource(kind="sessions")
    changes = HistoryChanges(root)
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(source.lock_path(root)):
        changes.mark(source)
    code = (
        "import sys; from pathlib import Path; "
        "from meridian.lib.platform.locking import lock_file;\n"
        f"with lock_file(Path({str(source.lock_path(root))!r})):\n"
        " print('locked', flush=True)\n"
        " sys.stdin.read()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        yield child
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=5)
        assert child.returncode == 0
        assert child.stdout is not None
        child.stdout.close()


@pytest.mark.parametrize("nested", [False, True])
def test_discovery_never_decodes_other_sessions_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    project, root = _runtime(tmp_path)
    parent = _spawn(root, "p-parent")
    middle = _spawn(root, "p-middle", parent=parent)
    spawn_store.finalize_spawn(root, middle, "succeeded", 0, origin="runner")
    child = _spawn(root, "p-child", owner="c-child" if nested else "c-owner", parent=middle)
    sibling = _spawn(root, "p-sibling")
    unrelated = _spawn(root, "p-unrelated", owner="c-other")
    spawn_store.finalize_spawn(root, unrelated, "succeeded", 0, origin="runner")
    history_index.HistoryIndex(root).rebuild()
    original = SpawnRecord.model_validate_json

    def scoped_decode(raw: str | bytes) -> SpawnRecord:
        # Candidate decoding is an observable resource boundary: unrelated rows
        # must not be hydrated just to discard them after the query.
        assert json.loads(raw)["id"] != unrelated, "decoded another session's candidate"
        return original(raw)

    monkeypatch.setattr(SpawnRecord, "model_validate_json", scoped_decode)
    rows = spawn_api._discover_pending_spawns(
        project,
        root,
        "c-owner",
        exclude_spawn_id=parent,
        only_descendants_of=parent if nested else None,
    )
    assert {row.id for row in rows} == ({child} if nested else {child, sibling})


def test_nested_discovery_reaches_live_child_through_archived_parent(tmp_path: Path) -> None:
    project, root = _runtime(tmp_path)
    start_row(root, "p1", HarnessId.PI, None)
    start_row(root, "p2", HarnessId.CODEX, "p1")
    for spawn_id in ("p1", "p2"):
        spawn_store.finalize_spawn(root, spawn_id, "succeeded", 0, origin="runner")
    archive_history(root, destination=tmp_path / "archives", refs=("p2",), apply=True)
    start_row(root, "p3", HarnessId.CODEX, "p2")
    spawn_store.mark_finalizing(root, "p3")
    rows = spawn_api._discover_pending_spawns(
        project,
        root,
        "c-owner",
        exclude_spawn_id="p1",
        only_descendants_of="p1",
    )
    assert [row.id for row in rows] == ["p3"]


@pytest.mark.parametrize("hard_timeout", [False, True])
def test_busy_discovery_respects_wait_budget_without_claiming_empty_work(
    tmp_path: Path,
    hard_timeout: bool,
) -> None:
    project, root = _runtime(tmp_path)
    child = _spawn(root, "p-child")
    history_index.HistoryIndex(root).rebuild()
    payload = SpawnWaitInput(
        project_root=str(project),
        observe=False,
        timeout=0.001 if hard_timeout else None,
        timeout_explicit=hard_timeout,
        yield_after_secs=0.06,
    )
    with _busy_sessions(root):
        if hard_timeout:
            with pytest.raises(TimeoutError, match="discover"):
                spawn_api.spawn_wait_sync(payload, ctx=RuntimeContext(chat_id=ChatId("c-owner")))
        else:
            result = spawn_api.spawn_wait_sync(
                payload,
                ctx=RuntimeContext(chat_id=ChatId("c-owner")),
            )
            assert result.checkpoint
            assert result.to_cli_wire()["checkpoint_discovery_pending"] is True
            assert "discovery" in result.format_text().lower()
            assert "not known" in result.format_text().lower()
    assert spawn_store.get_spawn(root, child) is not None


def test_discovery_retries_contention_then_finds_the_same_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, root = _runtime(tmp_path)
    child_id = _spawn(root, "p-child")
    history_index.HistoryIndex(root).rebuild()
    monkeypatch.setattr(history_index, "QUERY_TIMEOUT", 0.03)
    # The query's first budget is consumed by a writer. The retry notice is the
    # synchronization point that releases it, without sleeps or simulated locks.
    with _busy_sessions(root) as writer:

        class ReleaseWriter(NullSink):
            def status(self, message: str) -> None:
                if "retry" in message.lower():
                    assert writer.stdin is not None
                    writer.stdin.close()
                    writer.wait(timeout=5)
                    monkeypatch.setattr(history_index, "QUERY_TIMEOUT", 2.0)

        result = spawn_api.spawn_wait_sync(
            SpawnWaitInput(project_root=str(project), yield_after_secs=1.0, observe=False),
            ctx=RuntimeContext(chat_id=ChatId("c-owner")),
            sink=ReleaseWriter(),
        )
        assert result.checkpoint
        assert result.checkpoint_pending_ids == (child_id,)
        assert not result.to_cli_wire().get("checkpoint_discovery_pending", False)


def test_literal_ids_do_not_depend_on_unrelated_history_source_locks(tmp_path: Path) -> None:
    project, root = _runtime(tmp_path)
    child = _spawn(root, "p-child")
    history_index.HistoryIndex(root).rebuild()
    with _busy_sessions(root):
        result = spawn_api.spawn_wait_sync(
            SpawnWaitInput(
                project_root=str(project), spawn_ids=(child,), yield_after_secs=0, observe=False
            ),
            ctx=RuntimeContext(chat_id=ChatId("c-owner")),
        )
        assert result.checkpoint_pending_ids == (child,)


def test_cold_wait_checkpoint_cancels_build_without_poisoning_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, root = _runtime(tmp_path)
    child = _spawn(root, "p-child")
    index = history_index.HistoryIndex(root)
    original = history_index.HistoryIndex._project

    with monkeypatch.context() as patch:

        def slow_project(
            self: history_index.HistoryIndex,
            db: Connection,
            source: HistorySource,
        ) -> bool:
            time.sleep(0.03)
            return original(self, db, source)

        patch.setattr(history_index.HistoryIndex, "_project", slow_project)
        result = spawn_api.spawn_wait_sync(
            SpawnWaitInput(project_root=str(project), yield_after_secs=0.01, observe=False),
            ctx=RuntimeContext(chat_id=ChatId("c-owner")),
        )
        assert result.checkpoint_discovery_pending
        assert not index.failure_path.exists()
    result = spawn_api.spawn_wait_sync(
        SpawnWaitInput(project_root=str(project), yield_after_secs=2.0, observe=False),
        ctx=RuntimeContext(chat_id=ChatId("c-owner")),
    )
    assert result.checkpoint_pending_ids == (child,)
    assert not index.failure_path.exists()


def test_invalid_index_is_not_retried_as_transient_contention(tmp_path: Path) -> None:
    project, root = _runtime(tmp_path)
    _spawn(root, "p-child")
    index = history_index.HistoryIndex(root)
    index.directory.mkdir(parents=True, exist_ok=True)
    index.path.write_bytes(b"not a database")
    with pytest.raises(history_index.HistoryIndexIncomplete, match="corrupt"):
        spawn_api.spawn_wait_sync(
            SpawnWaitInput(project_root=str(project), observe=False),
            ctx=RuntimeContext(chat_id=ChatId("c-owner")),
        )
