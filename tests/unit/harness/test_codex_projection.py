from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.codex import project_codex_primary_preview
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _spec(*, interactive: bool) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        prompt="test",
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
        interactive=interactive,
    )


def test_interactive_codex_app_server_injects_idle_notify() -> None:
    command = project_codex_spec_to_appserver_command(
        _spec(interactive=True),
        host="127.0.0.1",
        port=1234,
    )

    notify_override = next(argument for argument in command if argument.startswith("notify="))
    key, separator, raw_command = notify_override.partition("=")
    assert key == "notify"
    assert separator == "="
    assert json.loads(raw_command) == [
        "meridian",
        "idle",
        "event",
        "--harness",
        "codex",
    ]


def test_noninteractive_codex_app_server_does_not_inject_idle_notify() -> None:
    command = project_codex_spec_to_appserver_command(
        _spec(interactive=False),
        host="127.0.0.1",
        port=1234,
    )

    assert not any(argument.startswith("notify=") for argument in command)


def test_codex_primary_preview_shows_idle_notify_on_managed_backend(tmp_path: Path) -> None:
    preview = project_codex_primary_preview(
        _spec(interactive=True),
        project_root=tmp_path,
    )

    assert "--listen" in preview.backend_command
    assert "ws://127.0.0.1:<port>" in preview.backend_command
    assert any(argument.startswith("notify=") for argument in preview.backend_command)
    assert preview.bootstrap_path == "thread/start"
    assert preview.attach_command[:2] == ("codex", "resume")
