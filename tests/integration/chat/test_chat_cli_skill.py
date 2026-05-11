# qa-validated: test-suite-redesign
"""Chat CLI dev mode, flag validation, stale asset warning, and headless/open tests."""

from __future__ import annotations

import importlib
import os
from io import StringIO
from pathlib import Path

import pytest

from meridian.cli import chat_cmd
from meridian.cli.chat_cmd import run_chat_server
from meridian.cli.output import OutputConfig
from meridian.lib.chat.policy import default_chat_policy_snapshot

cli_main = importlib.import_module("meridian.cli.main")


class _UnexpectedCall(RuntimeError):
    pass


class _FakeLauncher:
    pass


@pytest.fixture(autouse=True)
def _stable_policy_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub policy resolution for dev mode and flag tests — they don't exercise it."""
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_chat_policy_snapshot",
        lambda **_kwargs: default_chat_policy_snapshot(),
    )


def _write_dist(root: Path) -> Path:
    dist = root / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>chat ui</html>", encoding="utf-8")
    (assets / "index.js").write_text("console.log('chat')", encoding="utf-8")
    return dist


def _write_dev_frontend(root: Path) -> Path:
    frontend_root = root / "meridian-web"
    (frontend_root / "node_modules").mkdir(parents=True)
    (frontend_root / "package.json").write_text('{"scripts":{"dev":"vite"}}', encoding="utf-8")
    return frontend_root


def test_stale_asset_warning_mentions_rebuild(monkeypatch, tmp_path) -> None:
    project = tmp_path / "meridian-cli"
    source_dir = tmp_path / "meridian-web" / "src"
    source_dir.mkdir(parents=True)
    dist = _write_dist(tmp_path)
    index = dist / "index.html"
    source_file = source_dir / "App.tsx"
    source_file.write_text("export function App() { return null }", encoding="utf-8")
    newer = index.stat().st_mtime + 10
    os.utime(source_file, (newer, newer))
    monkeypatch.setattr(chat_cmd, "require_established_project_root", lambda: project)

    stdout = StringIO()
    chat_cmd._check_stale_assets(
        chat_cmd.FrontendAssets(root=dist, index_html=index, assets_dir=dist / "assets"),
        stdout,
    )

    assert "Frontend source is newer than built assets" in stdout.getvalue()
    assert "pnpm build" in stdout.getvalue()


def test_chat_command_meridian_env_dev_enables_dev_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setenv("MERIDIAN_ENV", "dev")
    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    )
    try:
        chat_cmd._chat(port=8765)
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["dev"] is True
    assert captured["frontend_root"] is None


def test_chat_command_headless_takes_precedence_over_meridian_env_dev(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setenv("MERIDIAN_ENV", "dev")
    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    )
    try:
        chat_cmd._chat(port=8765, headless=True)
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["headless"] is True
    assert captured["dev"] is False


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        (
            {"dev": True, "frontend_dist": "/tmp/dist"},
            "Error: --frontend-dist cannot be combined with --dev.\n",
        ),
        (
            {"frontend_root": "/tmp/frontend"},
            "Error: --frontend-root is only valid with --dev.\n",
        ),
        ({"no_portless": True}, "Error: --no-portless is only valid with --dev.\n"),
        ({"tailscale": True}, "Error: --tailscale and --funnel are only valid with --dev.\n"),
        ({"funnel": True}, "Error: --tailscale and --funnel are only valid with --dev.\n"),
        (
            {"portless_force": True},
            "Error: --portless-force is only valid with portless dev mode.\n",
        ),
    ],
)
def test_chat_cli_rejects_invalid_flag_combinations_before_startup(
    monkeypatch, tmp_path, kwargs: dict[str, object], expected_error: str
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()
    launch_attempts: list[str] = []

    def forbid(*_args, **_kwargs):
        launch_attempts.append("called")
        raise _UnexpectedCall("startup collaborator should not run")

    monkeypatch.setattr(chat_cmd, "resolve_frontend_assets", forbid)

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            uvicorn_run=forbid,
            stdout=stdout,
            **kwargs,
        )

    assert exc_info.value.code == 1
    assert stdout.getvalue() == expected_error
    assert launch_attempts == []


def test_chat_cli_headless_rejects_dev_mode(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            headless=True,
            dev=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    assert stdout.getvalue() == "Error: --dev cannot be combined with --headless.\n"


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        (
            {"headless": True, "no_portless": True},
            "Error: --no-portless is only valid with --dev.\n",
        ),
        (
            {"headless": True, "tailscale": True},
            "Error: --tailscale and --funnel are only valid with --dev.\n",
        ),
        (
            {"headless": True, "funnel": True},
            "Error: --tailscale and --funnel are only valid with --dev.\n",
        ),
        (
            {"headless": True, "portless_force": True},
            "Error: dev frontend flags cannot be combined with --headless.\n",
        ),
    ],
)
def test_chat_cli_headless_rejects_dev_frontend_flags(
    monkeypatch, tmp_path, kwargs, expected_error: str
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(port=8765, stdout=stdout, **kwargs)

    assert exc_info.value.code == 1
    assert stdout.getvalue() == expected_error


def test_chat_cli_dev_mode_reports_missing_frontend_checkout_actionably(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(
        "meridian.lib.chat.dev_frontend.resolve_dev_frontend_root",
        lambda *, explicit=None, **kwargs: None,
    )
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(port=8765, dev=True, stdout=stdout)

    assert exc_info.value.code == 1
    output = stdout.getvalue()
    assert "Error: Dev frontend checkout not found." in output
    assert "--frontend-root /path/to/meridian-web" in output
    assert "MERIDIAN_DEV_FRONTEND_ROOT=/path/to/meridian-web" in output
    assert "meridian chat --headless" in output


def test_chat_cli_dev_mode_reports_prerequisite_failures(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    frontend_root = tmp_path / "meridian-web"
    frontend_root.mkdir()
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            dev=True,
            frontend_root=str(frontend_root),
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    expected = f"Error: Frontend root is missing package.json: {frontend_root.resolve()}\n"
    assert stdout.getvalue() == expected


def test_chat_cli_dev_mode_surfaces_launcher_configuration_errors(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    def raise_config_error(**_kwargs):
        from meridian.lib.chat.dev_frontend import DevFrontendConfigurationError

        raise DevFrontendConfigurationError("--tailscale/--funnel require portless")

    monkeypatch.setattr(
        "meridian.lib.chat.dev_frontend.resolve_dev_frontend_launcher",
        raise_config_error,
    )

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(port=8765, dev=True, stdout=stdout)

    assert exc_info.value.code == 1
    assert stdout.getvalue() == "Error: --tailscale/--funnel require portless\n"


def test_chat_cli_dev_mode_uses_frontend_root_launcher_supervisor_and_warning(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    frontend_root = _write_dev_frontend(tmp_path)
    launcher = _FakeLauncher()
    captured: dict[str, object] = {}

    class FakeSupervisor:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            captured["ran"] = True
            return 0

    from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint

    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(
        "meridian.lib.chat.dev_frontend.resolve_dev_frontend_launcher",
        lambda **_kwargs: launcher,
    )
    monkeypatch.setattr("meridian.lib.chat.dev_frontend.DevSupervisor", FakeSupervisor)
    stdout = StringIO()

    actual_port = run_chat_server(
        host="0.0.0.0",
        port=8765,
        dev=True,
        frontend_root=str(frontend_root),
        open_browser=True,
        entrypoint=ChatEntryPoint(
            context=ApplicationContext(project_root=tmp_path, runtime_root=runtime_root),
            services=ApplicationServices(),
        ),
        stdout=stdout,
    )

    assert actual_port == 8765
    assert captured["backend_host"] == "0.0.0.0"
    assert captured["backend_port"] == 8765
    assert captured["frontend_root"] == frontend_root.resolve()
    assert captured["open_browser"] is True
    assert captured["launcher"] is launcher
    assert captured["ran"] is True
    assert stdout.getvalue() == (
        "Warning: Backend is bound to all interfaces."
        " The frontend sharing mode does not restrict backend API access.\n"
    )
    discovery = runtime_root / "chat-server.json"
    assert discovery.exists()
    assert '"url": "http://127.0.0.1:8765"' in discovery.read_text(encoding="utf-8")


def test_chat_cli_dev_mode_surfaces_frontend_launch_errors(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    frontend_root = _write_dev_frontend(tmp_path)

    class FakeSupervisor:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self) -> int:
            from meridian.lib.chat.dev_frontend.launcher import FrontendLaunchError

            raise FrontendLaunchError("portless failed to start")

    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr("meridian.lib.chat.dev_frontend.DevSupervisor", FakeSupervisor)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            dev=True,
            frontend_root=str(frontend_root),
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    assert stdout.getvalue() == "Error: portless failed to start\n"


def test_chat_cli_headless_warns_on_open(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()
    opened: list[str] = []
    monkeypatch.setattr(chat_cmd.webbrowser, "open", lambda url: opened.append(url))

    actual_port = run_chat_server(
        port=8765,
        headless=True,
        open_browser=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert actual_port == 8765
    assert opened == []
    assert stdout.getvalue() == (
        "Warning: --open is ignored in headless mode.\nChat backend: http://127.0.0.1:8765\n"
    )
