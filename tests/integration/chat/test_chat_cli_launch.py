# qa-validated: test-suite-redesign
"""Chat CLI launch plan construction, command routing, and subcommand validation tests."""

from __future__ import annotations

import importlib
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from meridian.cli import chat_cmd
from meridian.cli.chat_cmd import run_chat_server
from meridian.cli.output import OutputConfig
from meridian.lib.chat.policy import build_chat_backend_launch_plan, default_chat_policy_snapshot
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import CompositionWarning, ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint

cli_main = importlib.import_module("meridian.cli.main")


class EmptyPipelineLookup:
    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot or default_chat_policy_snapshot()

    def get_pipeline(self, chat_id: str):
        _ = chat_id
        return None

    def get_policy_snapshot(self, chat_id: str):
        _ = chat_id
        return self._snapshot


@pytest.fixture(autouse=True)
def _stable_policy_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub policy resolution for all launch tests — they don't exercise it."""
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_chat_policy_snapshot",
        lambda **_kwargs: default_chat_policy_snapshot(),
    )


def test_chat_command_falls_back_to_globally_parsed_harness(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="codex")
    )
    try:
        chat_cmd._chat(port=8765)
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["harness"] == "codex"
    assert captured["headless"] is False
    assert captured["frontend_dist"] is None
    assert captured["open_browser"] is False
    assert captured["autocompact"] is None


def test_chat_command_prefers_explicit_harness_over_global_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    )
    try:
        chat_cmd._chat(
            harness="opencode",
            port=8765,
            headless=True,
            frontend_dist="/tmp/dist",
            open_browser=True,
        )
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["harness"] == "opencode"
    assert captured["headless"] is True
    assert captured["frontend_dist"] == "/tmp/dist"
    assert captured["open_browser"] is True


def test_chat_command_passes_autocompact_to_chat_server(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    chat_cmd._chat(port=8765, autocompact=55)

    assert captured["autocompact"] == 55


@pytest.mark.parametrize("harness", [HarnessId.CLAUDE, HarnessId.CODEX, HarnessId.OPENCODE])
def test_backend_acquisition_preserves_requested_harness(tmp_path, harness: HarnessId) -> None:
    snapshot = default_chat_policy_snapshot(harness=harness, model="model-x")
    acquisition = chat_cmd._build_backend_acquisition(
        runtime_root=tmp_path / "runtime",
        project_root=tmp_path,
        pipeline_lookup=EmptyPipelineLookup(snapshot),
    )

    plan = acquisition._build_launch_plan("c1", "hello")
    config = plan.connection_config
    spec = plan.spec

    assert config.harness_id == harness
    assert spec.model == "model-x"


@pytest.mark.parametrize(
    ("harness_name", "expected_harness"),
    [
        ("claude", HarnessId.CLAUDE),
        ("codex", HarnessId.CODEX),
        ("opencode", HarnessId.OPENCODE),
    ],
)
def test_chat_cli_builds_runtime_with_factory_inputs(
    monkeypatch, tmp_path, harness_name: str, expected_harness: HarnessId
) -> None:
    from meridian.lib.catalog.model_aliases import AliasEntry

    captured: dict[str, object] = {}
    snapshot = default_chat_policy_snapshot(harness=expected_harness, model="model-x")
    runtime = object()
    entrypoint = ChatEntryPoint(
        context=ApplicationContext(
            project_root=tmp_path,
            runtime_root=tmp_path / "runtime",
        ),
        services=ApplicationServices(),
    )

    def fake_build_chat_runtime_from_entrypoint(
        *,
        entrypoint,
        default_policy_snapshot,
        backend_acquisition=None,
        acquisition_factory=None,
    ):
        _ = backend_acquisition
        captured["entrypoint"] = entrypoint
        captured["default_policy_snapshot"] = default_policy_snapshot
        captured["acquisition_factory"] = acquisition_factory
        return runtime

    def fake_configure(*, runtime) -> None:
        captured["configured_runtime"] = runtime

    monkeypatch.setattr(
        chat_cmd,
        "build_chat_runtime_from_entrypoint",
        fake_build_chat_runtime_from_entrypoint,
    )
    monkeypatch.setattr(chat_cmd, "_resolve_chat_policy_snapshot", lambda **_kwargs: snapshot)

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", fake_configure)
    monkeypatch.setattr(chat_server, "app", object())

    run_chat_server(
        harness=harness_name,
        model="model-x",
        port=8900,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=StringIO(),
        entrypoint=entrypoint,
    )

    assert captured["entrypoint"] is entrypoint
    assert captured["default_policy_snapshot"] == snapshot
    assert captured["configured_runtime"] is runtime

    factory = cast("chat_cmd._ChatBackendAcquisitionFactory", captured["acquisition_factory"])
    assert factory.policy_snapshot == snapshot

    lookup = EmptyPipelineLookup(snapshot)
    acquisition = factory.build(
        pipeline_lookup=lookup,
        project_root=tmp_path,
        runtime_root=cast("Path", entrypoint.context.runtime_root),
    )
    plan = acquisition._build_launch_plan("c1", "hello")
    config = plan.connection_config
    spec = plan.spec

    assert config.harness_id == expected_harness
    assert config.project_root == tmp_path
    assert spec.model == "model-x"


def test_chat_cli_blocks_nested_launch(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    configured: list[object] = []
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(chat_server, "app", object())

    with pytest.raises(
        ValueError,
        match="blocked in nested/delegated Meridian execution",
    ):
        run_chat_server(
            port=8765,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )

    assert configured == []
    assert not (runtime_root / "chat-server.json").exists()


def test_chat_cli_prints_policy_warnings_before_backend_url(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    snapshot = default_chat_policy_snapshot().model_copy(
        update={
            "warnings": (
                CompositionWarning(code="profile_warning", message="profile warning"),
                CompositionWarning(code="missing_skills_warning", message="missing skill warning"),
            )
        }
    )
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(chat_cmd, "_resolve_chat_policy_snapshot", lambda **_kwargs: snapshot)
    stdout = StringIO()

    run_chat_server(
        port=8765,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert stdout.getvalue() == (
        "Warning (profile_warning): profile warning\n"
        "Warning (missing_skills_warning): missing skill warning\n"
        "Chat backend: http://127.0.0.1:8765\n"
    )


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["chat", "ls", "--model", "codex"], "--model"),
        (["chat", "show", "c1", "--approval", "auto"], "--approval"),
        (["chat", "log", "c1", "--skills", "md-validation"], "--skills"),
        (["chat", "close", "c1", "--agent", "reviewer"], "--agent"),
    ],
)
def test_chat_management_subcommands_reject_launch_policy_flags(
    argv: list[str], flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert 'Unknown option: "' in stderr
    assert flag in stderr or (
        flag == "--agent" and '"-a"' in stderr
    )


@pytest.mark.parametrize(
    ("argv", "selector"),
    [
        (["--harness", "codex", "chat", "ls"], "--harness"),
        (["codex", "chat", "ls"], "codex"),
    ],
)
def test_chat_management_subcommands_reject_global_harness_selectors(
    argv: list[str], selector: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    message = str(exc_info.value)
    assert "Unknown option" in message
    assert selector in message
    assert capsys.readouterr().err == ""
