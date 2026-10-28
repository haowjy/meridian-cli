# qa-validated: test-suite-redesign
"""Contract and low-level bootstrap tests for primary process launch.

Tests that verify the execute_primary_process contract logic (bootstrap mode,
attach failure policy) and the SubprocessProcessLauncher output capture.
These tests do NOT spin up a real harness process.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import BootstrapMode
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.native_run import NativeRun
from meridian.lib.launch.process import runner as process_runner
from meridian.lib.launch.process.ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    LaunchedProcess,
    TerminalFds,
)
from meridian.lib.launch.process.primary_attach import PrimaryAttachError
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.session_scope import SessionAttempt
from meridian.lib.platform.launch_gate import ProcessIdentity
from meridian.lib.state import session_store
from meridian.lib.state.native_binding import Bound, Conflict
from meridian.lib.state.paths import LaunchPaths


def test_subprocess_launcher_captures_output_log(tmp_path: Path) -> None:
    output_log_path = tmp_path / "history.jsonl"
    running = SubprocessProcessLauncher().start(
        command=(
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.write('line-1\\n');"
                "sys.stdout.flush();"
                "sys.stderr.write('line-2\\n');"
                "sys.stderr.flush()"
            ),
        ),
        cwd=tmp_path,
        env=dict(os.environ),
        output_log_path=output_log_path,
        terminal_fds=TerminalFds(stdin=0, stdout=1, stderr=2),
    )
    launched = running.wait()

    assert launched.exit_code == 0
    assert output_log_path.read_text(encoding="utf-8").splitlines() == ["line-1", "line-2"]


def test_subprocess_launcher_mirrors_logged_stderr_to_terminal_fd(tmp_path: Path) -> None:
    terminal_read_fd, terminal_write_fd = os.pipe()
    stderr_log_path = tmp_path / "stderr.log"
    env = dict(os.environ)
    env[PRIMARY_STDERR_LOG_PATH_ENV] = str(stderr_log_path)
    try:
        running = SubprocessProcessLauncher().start(
            command=(
                sys.executable,
                "-c",
                "import os; os.write(2, b'tui stderr\\n')",
            ),
            cwd=tmp_path,
            env=env,
            output_log_path=None,
            terminal_fds=TerminalFds(
                stdin=0,
                stdout=1,
                stderr=terminal_write_fd,
            ),
        )
        launched = running.wait()
    finally:
        os.close(terminal_write_fd)

    try:
        mirrored = os.read(terminal_read_fd, 4096)
    finally:
        os.close(terminal_read_fd)

    assert launched.exit_code == 0
    assert mirrored == b"tui stderr\n"
    assert stderr_log_path.read_bytes() == b"tui stderr\n"


@pytest.mark.parametrize("capture_output", (False, True))
def test_subprocess_launcher_cancel_wait_releases_pipe_relays(
    tmp_path: Path,
    capture_output: bool,
) -> None:
    env = dict(os.environ)
    output_log_path = tmp_path / "output.log" if capture_output else None
    if not capture_output:
        env[PRIMARY_STDERR_LOG_PATH_ENV] = str(tmp_path / "stderr.log")
    running = SubprocessProcessLauncher().start(
        command=(sys.executable, "-c", "import time;time.sleep(60)"),
        cwd=tmp_path,
        env=env,
        output_log_path=output_log_path,
        terminal_fds=TerminalFds(stdin=0, stdout=1, stderr=2),
    )
    results: list[LaunchedProcess] = []
    wait_started = threading.Event()
    wait_finished = threading.Event()

    def _wait() -> None:
        wait_started.set()
        results.append(running.wait())
        wait_finished.set()

    wait_thread = threading.Thread(target=_wait)
    wait_thread.start()
    assert wait_started.wait(timeout=5.0)

    try:
        running.cancel_wait()
        assert wait_finished.wait(timeout=5.0)
        wait_thread.join()

        assert wait_thread.is_alive() is False
        assert [result.exit_code for result in results] == [130]
    finally:
        running.terminate()
        running.process.wait(timeout=5.0)


def test_execute_primary_process_uses_contract_bootstrap_mode_not_harness_id(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.spawn_store import start_spawn

    start_spawn(
        tmp_path,
        spawn_id=SpawnId("p-contract-blackbox"),
        chat_id="c1",
        model="test",
        agent="test",
        harness="codex",
        prompt="test",
        status="running",
    )
    log_dir = tmp_path / "spawns" / "p-contract-blackbox"
    harness_registry = get_default_harness_registry()
    harness_contract = harness_registry.get_contract(HarnessId.CODEX).model_copy(
        update={"bootstrap_mode": BootstrapMode.SUBPROCESS_ONLY}
    )
    black_box_calls = 0

    def _black_box(
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Path,
    ) -> process_runner.PrimaryProcessResult:
        nonlocal black_box_calls
        _ = (command, cwd, env, output_log_path)
        assert launcher_diagnostics_path == LaunchPaths(log_dir).launcher_log
        black_box_calls += 1
        if callable(on_child_started):
            on_child_started(ProcessIdentity(111, 100.0, 111))
        return process_runner.PrimaryProcessResult(0, 111, None)

    result = process_runner._execute_primary_process(
        harness_id=HarnessId.CODEX,
        primary_spawn_id=SpawnId("p-contract-blackbox"),
        log_dir=log_dir,
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=ResolvedLaunchSpec(
            prompt="hello",
            interactive=True,
        ),
        command=("codex",),
        harness_contract=harness_contract,
        native_run=_native_run(tmp_path),
        run_primary_process_with_capture_fn=_black_box,
        run_primary_attach_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess_only contract should bypass managed attach")
        ),
        on_running=lambda _pid: None,
    )

    assert black_box_calls == 1
    assert result.exit_code == 0
    assert result.managed_session_id is None
    assert result.cancelled is False
    assert result.launcher_diagnostics_path is None


def test_contract_attach_fallback_does_not_reuse_stale_launcher_diagnostics(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.spawn_store import start_spawn

    start_spawn(
        tmp_path,
        spawn_id=SpawnId("p-contract-fallback"),
        chat_id="c1",
        model="test",
        agent="test",
        harness="codex",
        prompt="test",
        status="running",
    )
    log_dir = tmp_path / "spawns" / "p-contract-fallback"
    harness_registry = get_default_harness_registry()
    harness_contract = harness_registry.get_contract(HarnessId.CLAUDE).model_copy(
        update={
            "bootstrap_mode": BootstrapMode.MANAGED_PRIMARY_ATTACH,
            "primary_attach_failure_policy": "fallback_to_blackbox",
        }
    )
    black_box_calls = 0
    launcher_diagnostics_path = LaunchPaths(log_dir).launcher_log
    launcher_diagnostics_path.write_text("stale relay failure\n", encoding="utf-8")

    def _black_box(
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Path,
    ) -> process_runner.PrimaryProcessResult:
        nonlocal black_box_calls
        _ = (command, cwd, env, output_log_path)
        assert launcher_diagnostics_path == LaunchPaths(log_dir).launcher_log
        black_box_calls += 1
        if callable(on_child_started):
            on_child_started(ProcessIdentity(222, 100.0, 222))
        return process_runner.PrimaryProcessResult(0, 222, None)

    result = process_runner._execute_primary_process(
        harness_id=HarnessId.CLAUDE,
        primary_spawn_id=SpawnId("p-contract-fallback"),
        log_dir=log_dir,
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=ResolvedLaunchSpec(
            prompt="hello",
            interactive=True,
        ),
        command=("claude",),
        harness_contract=harness_contract,
        native_run=_native_run(tmp_path),
        run_primary_process_with_capture_fn=_black_box,
        run_primary_attach_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            PrimaryAttachError("fallback please")
        ),
        on_running=lambda _pid: None,
    )

    assert black_box_calls == 1
    assert result.exit_code == 0
    assert result.managed_session_id is None
    assert result.cancelled is False
    assert result.launcher_diagnostics_path is None


@pytest.mark.parametrize("source", ["assigned", "observed"])
def test_binding_mirrors_only_the_first_identity(
    tmp_path: Path,
    source: Literal["assigned", "observed"],
) -> None:
    from structlog.testing import capture_logs

    chat_id = session_store.start_session(tmp_path, "claude", "", "sonnet").chat_id
    record = session_store.chat_record(tmp_path, chat_id)
    assert record is not None
    attempt = SessionAttempt(tmp_path, chat_id, record.session_instance_id, None, record)
    try:
        first = attempt.bind(NativeKeyFields(session_id="first"), source)
        assert isinstance(first, Bound)
        with capture_logs() as logs:
            conflict = attempt.bind(NativeKeyFields(session_id="other"), "observed")
            assert isinstance(conflict, Conflict)
            assert conflict.kept.session_id == "first"
        assert len([log for log in logs if log["event"] == "native_binding_conflict"]) == 1
        assert session_store.get_session_harness_id(tmp_path, chat_id) == "first"
    finally:
        session_store.stop_session(tmp_path, chat_id)


def _native_run(root: Path) -> NativeRun:
    chat_id = session_store.start_session(root, "codex", "", "").chat_id
    record = session_store.chat_record(root, chat_id)
    assert record is not None
    attempt = SessionAttempt(root, chat_id, record.session_instance_id, None, record)
    return NativeRun(attempt, None, NativeKeyFields("codex"), None, None)


def test_native_observer_acks_each_publication_and_dies_with_its_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from meridian.lib.core.native_identity import (
        NativeKey,
        NativeObservation,
        NativeObservationBatch,
    )
    from meridian.lib.state import spawn_store

    def observers() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name.startswith("meridian-native-observer")]

    chat = session_store.start_session(tmp_path, "claude", "entry", "", native_store="/s").chat_id
    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id=chat,
        harness="claude",
        kind="primary",
        harness_session_id="entry",
        model="",
        agent="",
        prompt="",
    )
    record = session_store.chat_record(tmp_path, chat)
    assert record is not None
    attempt = SessionAttempt(
        tmp_path, chat, record.session_instance_id, None, record, SpawnId("p1")
    )
    run = NativeRun(attempt, None, NativeKeyFields("claude", "/s", "entry"), None, None)
    with run.observing(
        cast("Any", SimpleNamespace(native_observation_reader=lambda **_: None)),
        child_env={},
    ) as start:
        start(1)
        assert observers() == []  # Unsupported adapters never idle a thread.

    publish = session_store.get_or_create_observed_chat
    published: list[str] = []

    def flaky_publish(*args: Any, **kwargs: Any) -> str | None:
        published.append(args[4])
        if published == ["a", "b"]:
            raise OSError("journal busy")
        return publish(*args, **kwargs)

    def failing_diagnostic(self: NativeRun, chat_id: str) -> None:
        raise RuntimeError("diagnostics unavailable")

    monkeypatch.setattr(session_store, "get_or_create_observed_chat", flaky_publish)
    monkeypatch.setattr(NativeRun, "_warn_overlap", failing_diagnostic)
    facts = tuple(
        NativeObservation(NativeKey("claude", "/s", sid), None, cursor)
        for cursor, sid in enumerate(("a", "b"), start=1)
    )
    reads: list[int] = []

    def reader(cursor: int) -> NativeObservationBatch:
        reads.append(cursor)
        pending = tuple(fact for fact in facts if fact.next_cursor > cursor)
        return NativeObservationBatch(pending, pending[-1].next_cursor if pending else cursor)

    with (
        pytest.raises(RuntimeError, match="caller failed"),
        run.observing(
            cast("Any", SimpleNamespace(native_observation_reader=lambda **_: reader)),
            child_env={},
        ) as start,
    ):
        start(123)
        for _ in range(200):
            if len(published) == 3:
                break
            threading.Event().wait(0.025)
        raise RuntimeError("caller failed")

    assert observers() == []
    assert published == ["a", "b", "b"]  # "a" was acknowledged, never replayed.
    assert reads[:2] == [0, 1]
    row = spawn_store.get_spawn(tmp_path, "p1")
    assert row is not None and row.current_chat_id is not None
    current = session_store.chat_record(tmp_path, row.current_chat_id)
    assert current is not None and current.harness_session_id == "b"
    assert run.observation_error is None
    session_store.stop_session(tmp_path, chat)
