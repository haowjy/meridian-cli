"""Unit tests for external hook runner timeout handling."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

from meridian.lib.hooks.runner import ExternalHookRunner
from meridian.lib.hooks.types import Hook, HookContext


def _context(project_root: Path, runtime_root: Path) -> HookContext:
    return HookContext(
        event_name="spawn.finalized",
        event_id=uuid4(),
        timestamp="2026-04-19T12:00:00+00:00",
        project_root=str(project_root),
        runtime_root=str(runtime_root),
        spawn_id="p123",
        spawn_status="success",
        spawn_agent="reviewer",
        spawn_model="gpt-5.3-codex",
    )


def _external_hook(command: str) -> Hook:
    return Hook(
        name="notify",
        event="spawn.finalized",
        source="project",
        command=command,
    )


def test_external_runner_marks_timeout_and_invokes_process_tree_teardown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    teardown_calls: list[int] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = -15
            self.pid = 4242
            self.calls = 0

        def poll(self) -> int | None:
            return None

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    cmd="hook",
                    timeout=1,
                    output=b"partial-out",
                    stderr=b"partial-err",
                )
            return (b"drained-out", b"drained-err")

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._process = FakeProcess()

        def __getattr__(self, name: str) -> object:
            return getattr(self._process, name)

    def _fake_terminate_process_tree(
        process: subprocess.Popen[bytes],
        *,
        grace_secs: float,
    ) -> None:
        teardown_calls.append(process.pid)  # type: ignore[arg-type]

    monkeypatch.setattr("meridian.lib.hooks.runner.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "meridian.lib.hooks.runner._terminate_process_tree",
        _fake_terminate_process_tree,
    )

    runner = ExternalHookRunner(project_root)
    result = runner.run(
        _external_hook("ignored"),
        _context(project_root, tmp_path / "state"),
        timeout_secs=1,
    )

    assert result.outcome == "timeout"
    assert result.success is False
    assert result.error == "Timed out after 1s."
    assert result.exit_code == -15
    assert result.stdout == "partial-outdrained-out"
    assert result.stderr == "partial-errdrained-err"
    assert teardown_calls == [4242]


def test_external_runner_timeout_escalates_to_kill_when_drain_times_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    killed = False

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.pid = 5151
            self.calls = 0

        def poll(self) -> int | None:
            return None

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    cmd="hook",
                    timeout=1,
                    output=b"partial-out",
                    stderr=b"partial-err",
                )
            if self.calls == 2:
                raise subprocess.TimeoutExpired(
                    cmd="hook",
                    timeout=2.0,
                    output=b"term-out",
                    stderr=b"term-err",
                )
            return (b"kill-out", b"kill-err")

        def kill(self) -> None:
            nonlocal killed
            killed = True
            self.returncode = -9

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._process = FakeProcess()

        def __getattr__(self, name: str) -> object:
            return getattr(self._process, name)

    monkeypatch.setattr("meridian.lib.hooks.runner.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "meridian.lib.hooks.runner._terminate_process_tree",
        lambda *_args, **_kwargs: None,
    )

    runner = ExternalHookRunner(project_root)
    result = runner.run(
        _external_hook("ignored"),
        _context(project_root, tmp_path / "state"),
        timeout_secs=1,
    )

    assert result.outcome == "timeout"
    assert result.exit_code == -9
    assert result.stdout == "partial-outterm-outkill-out"
    assert result.stderr == "partial-errterm-errkill-err"
    assert killed is True
