"""History-index readers distinguish lock-gated and append-only sources."""

from __future__ import annotations

import select
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select as sql_select

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.sink import NullSink
from meridian.lib.core.types import ChatId
from meridian.lib.ops.spawn.models import SpawnWaitInput
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import history_index, spawn_store
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.jsonl import append_durable_jsonl_line
from meridian.lib.state.session_fold import SessionStartEvent


def _spawn(root: Path, spawn_id: str = "p-busy") -> str:
    return str(
        spawn_store.start_spawn(
            root,
            spawn_id=spawn_id,
            chat_id="c9008",
            owner_chat_id="c9008",
            model="test",
            agent="coder",
            harness="pi",
            prompt="history contention",
        )
    )


@contextmanager
def _held_lock(
    lock_path: Path,
    *,
    release_after_secs: float | None,
) -> Generator[None]:
    child_code = """
import sys
import threading
from pathlib import Path
from meridian.lib.platform.locking import lock_file

with lock_file(Path(sys.argv[1])):
    print("locked", flush=True)
    if sys.argv[2] == "stdin":
        sys.stdin.read()
    else:
        threading.Event().wait(float(sys.argv[2]))
"""
    hold_for = "stdin" if release_after_secs is None else str(release_after_secs)
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(lock_path), hold_for],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        ready, _, _ = select.select([child.stdout], [], [], 10.0)
        assert ready, "timed out waiting for history source lock holder"
        assert child.stdout.readline().strip() == "locked"
        yield
    finally:
        if child.poll() is None:
            assert child.stdin is not None
            child.stdin.close()
            child.wait(timeout=10)
        stderr = child.stderr.read() if child.stderr is not None else ""
        assert child.returncode == 0, stderr
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


@contextmanager
def _held_source(
    root: Path,
    source: HistorySource,
    *,
    release_after_secs: float | None,
) -> Generator[None]:
    """Publish a marker, then hold its real source lock in another process."""
    changes = HistoryChanges(root)
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(source.lock_path(root)):
        changes.mark(source)
    with _held_lock(source.lock_path(root), release_after_secs=release_after_secs):
        yield


def test_query_projects_committed_sessions_without_waiting_for_writer(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _spawn(root)
    index = history_index.HistoryIndex(root)
    index.rebuild()
    source = HistorySource(kind="sessions")
    event = SessionStartEvent(
        chat_id="c-committed",
        kind="primary",
        harness="codex",
        harness_session_id="native-committed",
        model="test",
        session_instance_id="generation-committed",
        started_at="2026-10-15T12:00:00Z",
    )
    with (
        lock_file(HistoryChanges(root).mutation_lock, mode="shared"),
        lock_file(source.lock_path(root)),
    ):
        HistoryChanges(root).mark(source)
        append_durable_jsonl_line(root / "sessions.jsonl", event.model_dump_json())
    marker_path = HistoryChanges(root).directory / source.name

    with _held_source(root, source, release_after_secs=None):
        with index.query() as db:
            assert db.execute(
                sql_select(history_index.SESSIONS.c.chat).where(
                    history_index.SESSIONS.c.chat == event.chat_id
                )
            ).scalar_one() == event.chat_id
        assert marker_path.exists()
        assert not index.failure_path.exists()

    index.catch_up()
    assert not marker_path.exists()


def test_no_arg_wait_caps_discovery_at_query_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "runtime"
    _spawn(root)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MERIDIAN_HOME", str(home / ".meridian"))
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", str(root))
    for name in ("MERIDIAN_PROJECT_DIR", "MERIDIAN_SPAWN_ID", "_MERIDIAN_DEPTH"):
        monkeypatch.delenv(name, raising=False)

    deadlines: list[float] = []

    def discover(
        *args: object, deadline: float, **kwargs: object
    ) -> spawn_api._WaitTargetDiscovery:
        deadlines.append(deadline)
        return spawn_api._WaitTargetDiscovery(discovery_pending=True)

    monkeypatch.setattr(spawn_api, "_resolve_wait_targets", discover)
    monkeypatch.setattr(spawn_api, "time", SimpleNamespace(monotonic=lambda: 100.0))

    result = spawn_api.spawn_wait_sync(
        SpawnWaitInput(
            project_root=str(project),
            yield_after_secs=3_000.0,
            observe=False,
        ),
        ctx=RuntimeContext(chat_id=ChatId("c9008")),
    )

    assert result.checkpoint_discovery_pending
    assert deadlines == [100.0 + history_index.QUERY_TIMEOUT]


def test_busy_message_names_lock_gate_without_blame_for_pending_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    _spawn(root)
    index = history_index.HistoryIndex(root)
    index.rebuild()
    HistoryChanges(root).mark(HistorySource(kind="sessions"))
    monkeypatch.setattr(history_index, "QUERY_TIMEOUT", 0.1)

    with (
        _held_lock(index.catchup_lock, release_after_secs=None),
        pytest.raises(history_index.HistoryIndexBusy) as raised,
    ):
        index.spawns()

    message = str(raised.value)
    assert message == (
        "History index is busy (history index catch-up gate); another Meridian process "
        "holds it. Retry the command."
    )
    assert "sessions" not in message
    assert "is writing" not in message


def test_no_arg_wait_yields_when_discovery_remains_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "runtime"
    _spawn(root)
    history_index.HistoryIndex(root).rebuild()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MERIDIAN_HOME", str(home / ".meridian"))
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", str(root))
    for name in ("MERIDIAN_PROJECT_DIR", "MERIDIAN_SPAWN_ID", "_MERIDIAN_DEPTH"):
        monkeypatch.delenv(name, raising=False)

    class StatusSink(NullSink):
        def __init__(self) -> None:
            self.messages: list[str] = []

        def status(self, message: str) -> None:
            self.messages.append(message)

    sink = StatusSink()
    with _held_source(
        root,
        HistorySource(kind="spawn", key="p-busy"),
        release_after_secs=None,
    ):
        result = spawn_api.spawn_wait_sync(
            SpawnWaitInput(
                project_root=str(project),
                yield_after_secs=0.1,
                observe=False,
            ),
            ctx=RuntimeContext(chat_id=ChatId("c9008")),
            sink=sink,
        )

    assert result.checkpoint
    assert result.to_cli_wire()["checkpoint_discovery_pending"] is True
    assert "discovery still pending (history index busy)" in result.format_text().lower()
    assert "no pending spawns" not in "\n".join(sink.messages).lower()
