"""Tests for Windows UTF-8 stdout/stderr reconfiguration in the CLI entrypoint."""

from __future__ import annotations

import io
import os
import sys
import types

import pytest

import meridian.cli.entrypoint as entrypoint_module
import meridian.lib.core.sandbox_boundary as sandbox_boundary


class _FakeTextIOWrapper(io.TextIOWrapper):
    """Minimal stand-in for sys.stdout / sys.stderr with reconfigure support."""

    def __init__(self, encoding: str = "cp1252") -> None:
        super().__init__(io.BytesIO(), encoding=encoding)
        self._reconfigure_calls: list[dict[str, object]] = []

    def reconfigure(self, **kwargs: object) -> None:
        self._reconfigure_calls.append(kwargs)


def _make_fake_main_module(captured: dict[str, object]) -> types.ModuleType:
    fake = types.ModuleType("meridian.cli.main")

    def _noop(*, argv: list[str]) -> None:
        captured["argv"] = argv
        captured["managed"] = os.environ.get("MERIDIAN_MANAGED")

    fake.main = _noop  # type: ignore[attr-defined]
    return fake


# ---------------------------------------------------------------------------
# Windows reconfiguration — active on win32
# ---------------------------------------------------------------------------


def test_reconfigure_called_for_stdout_and_stderr_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both streams are reconfigured to utf-8 on Windows."""
    fake_stdout = _FakeTextIOWrapper(encoding="cp1252")
    fake_stderr = _FakeTextIOWrapper(encoding="cp1252")

    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setattr(entrypoint_module, "IS_WINDOWS", True)
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert fake_stdout._reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert fake_stderr._reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_reconfigure_not_called_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streams are not touched on non-Windows platforms."""
    fake_stdout = _FakeTextIOWrapper(encoding="utf-8")
    fake_stderr = _FakeTextIOWrapper(encoding="utf-8")

    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setattr(entrypoint_module, "IS_WINDOWS", False)
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert fake_stdout._reconfigure_calls == []
    assert fake_stderr._reconfigure_calls == []


def test_entrypoint_defaults_meridian_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.delenv("MERIDIAN_MANAGED", raising=False)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured["argv"] == ["spawn", "list"]
    assert captured["managed"] == "1"
    assert "MERIDIAN_MANAGED" not in os.environ


def test_entrypoint_preserves_outer_meridian_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setenv("MERIDIAN_MANAGED", "0")
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured["argv"] == ["spawn", "list"]
    assert captured["managed"] == "0"
    assert os.environ["MERIDIAN_MANAGED"] == "0"


@pytest.mark.parametrize(
    "args",
    [
        ["spawn", "--bg", "-m", "haiku", "-p", "child"],
        ["--format", "text", "spawn", "-p", "child", "--bg", "-m", "haiku"],
        ["spawn", "--bg", "-a", "explorer", "-p", "child"],
        ["spawn", "--bg", "--prompt-file", "/tmp/child.md"],
        ["spawn", "--bg", "--continue", "p123"],
        ["spawn", "--bg", "--fork", "p123", "-p", "child"],
        ["codex", "spawn", "--bg", "-m", "gpt-6-luna", "-p", "child"],
    ],
)
def test_entrypoint_refuses_sandboxed_spawn_before_full_cli_import(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setenv("_MERIDIAN_LAUNCHER_PID_NAMESPACE", "pid:[host]")
        monkeypatch.setattr(sandbox_boundary, "current_pid_namespace", lambda: "pid:[sandbox]")
        monkeypatch.setattr(sys, "argv", ["meridian", *args])

        with pytest.raises(SystemExit) as caught:
            entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert caught.value.code == 1
    assert captured == {}
    stderr = capsys.readouterr().err
    assert "Spawn setup failed before initialization" in stderr
    assert "standalone shell command" in stderr


@pytest.mark.parametrize("flag", ["--dry-run", "--dry-run=true", "--no-dry-run=false"])
def test_entrypoint_allows_sandboxed_spawn_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setenv("_MERIDIAN_LAUNCHER_PID_NAMESPACE", "pid:[host]")
        monkeypatch.setattr(sandbox_boundary, "current_pid_namespace", lambda: "pid:[sandbox]")
        monkeypatch.setattr(
            sys,
            "argv",
            ["meridian", "spawn", flag, "-p", "preview"],
        )

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured["argv"] == ["spawn", flag, "-p", "preview"]


def test_entrypoint_allows_sandboxed_spawn_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setenv("_MERIDIAN_LAUNCHER_PID_NAMESPACE", "pid:[host]")
        monkeypatch.setattr(sandbox_boundary, "current_pid_namespace", lambda: "pid:[sandbox]")
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "--help"])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured["argv"] == ["spawn", "--help"]


@pytest.mark.parametrize(
    "args",
    [
        ["spawn", "report", "show", "p1"],
        ["spawn", "files"],
        ["spawn", "log", "p1"],
        ["spawn", "unknown-command"],
    ],
)
def test_entrypoint_leaves_sandboxed_spawn_subcommands_to_full_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setenv("_MERIDIAN_LAUNCHER_PID_NAMESPACE", "pid:[host]")
        monkeypatch.setattr(sandbox_boundary, "current_pid_namespace", lambda: "pid:[sandbox]")
        monkeypatch.setattr(sys, "argv", ["meridian", *args])

        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    assert captured["argv"] == args


def test_entrypoint_refuses_sandboxed_positional_prompt_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_MERIDIAN_LAUNCHER_PID_NAMESPACE", "pid:[host]")
    monkeypatch.setattr(sandbox_boundary, "current_pid_namespace", lambda: "pid:[sandbox]")
    monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "review?"])

    with pytest.raises(SystemExit) as caught:
        entrypoint_module.main()

    assert caught.value.code == 1


def test_reconfigure_skipped_when_stream_lacks_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streams without reconfigure() are left alone (e.g. redirected pipes)."""

    class _NoReconfigure:
        def write(self, s: str) -> int:
            return len(s)

        def flush(self) -> None:
            pass

    fake_stdout = _NoReconfigure()
    fake_stderr = _NoReconfigure()

    captured: dict[str, object] = {}
    fake_main_module = _make_fake_main_module(captured)
    original = sys.modules.get("meridian.cli.main")
    sys.modules["meridian.cli.main"] = fake_main_module

    try:
        monkeypatch.setattr(entrypoint_module, "IS_WINDOWS", True)
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        monkeypatch.setattr(sys, "argv", ["meridian", "spawn", "list"])

        # Should not raise even though reconfigure is absent.
        entrypoint_module.main()
    finally:
        if original is not None:
            sys.modules["meridian.cli.main"] = original
        else:
            sys.modules.pop("meridian.cli.main", None)

    # No attribute error means the guard worked.
    assert not hasattr(fake_stdout, "_reconfigure_calls")
