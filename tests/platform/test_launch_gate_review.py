"""Named review regressions exercised at the real launcher seams."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil
import pytest

from meridian.lib.launch.process.ports import TerminalFds
from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.platform.launch_gate import LaunchGate
from tests.conftest import posix_only
from tests.platform.test_launch_gate import _alive, _until

pytestmark = posix_only


@pytest.mark.parametrize("launcher_type", [PtyProcessLauncher, SubprocessProcessLauncher])
@pytest.mark.parametrize("action", ["kill", "cancel"])
def test_wait_before_go_reaps_without_exec(tmp_path: Path, launcher_type, action: str) -> None:
    marker = tmp_path / "executed"
    with open(os.devnull, "r+b") as terminal:
        running = launcher_type().start(
            command=("/bin/sh", "-c", f"echo ran > {marker}"),
            cwd=tmp_path,
            env=dict(os.environ),
            output_log_path=tmp_path / "output",
            terminal_fds=TerminalFds(terminal.fileno(), terminal.fileno(), terminal.fileno()),
        )
        pid = running.identity.pid
        try:
            if action == "kill":
                os.kill(pid, signal.SIGKILL)
                _until(lambda: not _alive(pid))
            else:
                running.cancel_wait()
            result = running.wait()
            assert result.exit_code == (-signal.SIGKILL if action == "kill" else 125)
            with pytest.raises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            assert not marker.exists()
        finally:
            running.terminate()
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with suppress(ChildProcessError):
                os.waitpid(pid, 0)


def test_guardian_grace_ignores_hostile_path(tmp_path: Path) -> None:
    grace = 0.6
    marker = tmp_path / "ready"
    gate = LaunchGate(grace_seconds=grace)
    child = subprocess.Popen(
        **gate.spawn_kwargs(
            (
                sys.executable,
                "-c",
                "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"pathlib.Path({str(marker)!r}).touch(); time.sleep(60)",
            ),
            {"PATH": "/nonexistent"},
        ),
    )
    gate.adopt(child.pid)
    try:
        gate.open()
        _until(marker.exists)
        start = time.monotonic()
        gate.close()
        assert child.wait(timeout=5) == -signal.SIGKILL
        assert time.monotonic() - start >= grace * 0.9
    finally:
        gate.close()
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


@pytest.mark.parametrize("status", ["running", "finalizing", "terminal_claim"])
def test_live_primary_stale_heartbeat_survives_reconciliation(
    tmp_path: Path, status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.lib.core.types import SpawnId
    from meridian.lib.platform.process_scope import ProcessScopeSnapshot
    from meridian.lib.state import spawn_store
    from meridian.lib.state.process_scope_projection import record_scope
    from meridian.lib.state.reaper import reconcile_active_spawn
    from meridian.lib.state.reaper_cleanup_claim import claim_active_spawn_scopes

    monkeypatch.setattr("meridian.lib.state.reaper.is_root_side_effect_process", lambda: True)
    runtime = tmp_path / "runtime"
    spawn_id = SpawnId("p1")
    child = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        spawn_store.start_spawn(
            runtime,
            spawn_id=spawn_id,
            chat_id="c1",
            model="test",
            agent="test",
            harness="claude",
            prompt="test",
            status="running",
            kind="primary",
            started_at="2000-01-01T00:00:00Z",
        )
        spawn_store.update_spawn(
            runtime,
            spawn_id,
            runner_pid=os.getpid(),
            runner_created_at_epoch=psutil.Process().create_time(),
        )
        record_scope(
            runtime,
            spawn_id,
            ProcessScopeSnapshot(
                scope_id="tui",
                owner_policy="spawn_owned",
                owner_id=str(spawn_id),
                role="harness_tui",
                containment="posix_pgid",
                root_pid=child.pid,
                root_created_at_epoch=psutil.Process(child.pid).create_time(),
                pgid=child.pid,
                job_name=None,
                degraded_reason=None,
            ),
        )
        heartbeat = runtime / "spawns" / spawn_id / "heartbeat"
        heartbeat.touch()
        os.utime(heartbeat, (1, 1))
        if status == "finalizing":
            spawn_store.record_runner_exit(
                runtime,
                spawn_id,
                status="failed",
                exit_code=1,
                error="orphan_run",
                exited_at="2000-01-01T00:00:00Z",
            )
        elif status == "terminal_claim":
            claim_active_spawn_scopes(runtime, spawn_id)
            spawn_store.finalize_spawn(runtime, spawn_id, "failed", 1, origin="reconciler")
        record = spawn_store.get_spawn(runtime, spawn_id)
        assert record is not None
        reconcile_active_spawn(tmp_path, runtime, record)
        assert child.poll() is None
        assert os.getpgid(child.pid) == child.pid
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("unknown_identity", ["pid", "birth", "access_denied"])
def test_primary_unknown_runner_finalizes_without_signalling_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_identity: str
) -> None:
    from meridian.lib.core.types import SpawnId
    from meridian.lib.platform.process_scope import ProcessScopeSnapshot
    from meridian.lib.state import spawn_store
    from meridian.lib.state.process_scope_projection import record_scope
    from meridian.lib.state.reaper import reconcile_active_spawn

    monkeypatch.setattr("meridian.lib.state.reaper.is_root_side_effect_process", lambda: True)
    runtime = tmp_path / "runtime"
    spawn_id = SpawnId("p1")
    spawn_store.start_spawn(
        runtime,
        spawn_id=spawn_id,
        chat_id="c1",
        model="test",
        agent="test",
        harness="claude",
        prompt="test",
        kind="primary",
        status="running" if unknown_identity == "pid" else "finalizing",
        started_at="2000-01-01T00:00:00Z",
    )
    birth = psutil.Process().create_time()
    monkeypatch.setattr(spawn_store, "_runner_created_at_epoch_for_pid", lambda _pid: None)
    spawn_store.update_spawn(
        runtime,
        spawn_id,
        runner_pid=None if unknown_identity == "pid" else os.getpid(),
        runner_created_at_epoch=None if unknown_identity == "birth" else birth,
    )
    record_scope(
        runtime,
        spawn_id,
        ProcessScopeSnapshot(
            scope_id="tui",
            owner_policy="spawn_owned",
            owner_id=str(spawn_id),
            role="harness_tui",
            containment="posix_pgid",
            root_pid=os.getpid(),
            root_created_at_epoch=birth,
            pgid=os.getpgrp(),
            job_name=None,
            degraded_reason=None,
        ),
    )
    if unknown_identity == "access_denied":

        def denied(pid: int) -> psutil.Process:
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr("meridian.lib.state.liveness.psutil.Process", denied)

    def unexpected_signal(*args, **kwargs):
        pytest.fail("unproven runner death must not authorize scope termination")

    monkeypatch.setattr("meridian.lib.core.process_cleanup.terminate_scope_sync", unexpected_signal)
    record = spawn_store.get_spawn(runtime, spawn_id)
    assert record is not None
    finalized = reconcile_active_spawn(tmp_path, runtime, record)
    assert finalized.status == "failed"
    persisted = spawn_store.get_spawn(runtime, spawn_id)
    assert persisted is not None and persisted.status == "failed"
    # A retained cleanup claim must remain safe on a later terminal-row pass too.
    reconcile_active_spawn(tmp_path, runtime, persisted)
