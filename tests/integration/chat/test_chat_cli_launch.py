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
from meridian.lib.chat.policy import default_chat_policy_snapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.launch.launch_types import CompositionWarning
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


@pytest.mark.parametrize(
    ("harness_name", "expected_harness"),
    [
        ("claude", HarnessId.CLAUDE),
    ],
)
def test_chat_cli_builds_runtime_with_factory_inputs(
    monkeypatch, tmp_path, harness_name: str, expected_harness: HarnessId
) -> None:

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
    assert config.control_root == tmp_path
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


