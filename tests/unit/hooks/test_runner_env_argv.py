"""External hook runner argv and env hardening."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from meridian.lib.hooks.runner import ExternalHookRunner
from meridian.lib.hooks.types import Hook, HookContext


def test_external_hook_runs_argv_without_shell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        def communicate(self, **_kwargs: object) -> tuple[bytes, bytes]:
            return b"ok", b""

    def _fake_popen(
        command: object,
        *,
        shell: bool,
        env: dict[str, str],
        **_kwargs: object,
    ) -> _FakeProcess:
        captured["command"] = command
        captured["shell"] = shell
        captured["env"] = env
        return _FakeProcess()

    monkeypatch.setattr("meridian.lib.hooks.runner.subprocess.Popen", _fake_popen)
    monkeypatch.setenv("SECRET_API_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")

    hook = Hook(
        name="argv-hook",
        event="spawn.created",
        source="project",
        command_argv=("/bin/echo", "hi"),
    )
    context = HookContext.from_roots(
        event_name="spawn.created",
        event_id=uuid4(),
        timestamp="2026-01-01T00:00:00Z",
        project_root=str(tmp_path),
        runtime_root=str(tmp_path / ".meridian"),
    )
    result = ExternalHookRunner(tmp_path).run(hook, context, timeout_secs=5)

    assert result.success is True
    assert captured["shell"] is False
    assert captured["command"] == ["/bin/echo", "hi"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "SECRET_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
