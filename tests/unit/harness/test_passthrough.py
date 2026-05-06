from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ObserverEndpoint
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec, CodexLaunchSpec, OpenCodeLaunchSpec
from meridian.lib.harness.passthrough.base import PassthroughError
from meridian.lib.harness.passthrough.claude import ClaudePassthrough
from meridian.lib.harness.passthrough.codex import CodexPassthrough
from meridian.lib.harness.passthrough.opencode import OpenCodePassthrough
from meridian.lib.harness.passthrough.registry import get_passthrough
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _permission_resolver() -> UnsafeNoOpPermissionResolver:
    return UnsafeNoOpPermissionResolver(_suppress_warning=True)


@pytest.mark.parametrize(
    ("harness_id", "expected_type"),
    [
        (HarnessId.CLAUDE, ClaudePassthrough),
        (HarnessId.CODEX, CodexPassthrough),
        (HarnessId.OPENCODE, OpenCodePassthrough),
    ],
)
def test_passthrough_registry_returns_expected_implementation(
    harness_id: HarnessId, expected_type: type[object]
) -> None:
    assert isinstance(get_passthrough(harness_id), expected_type)


def test_passthrough_registry_rejects_unknown_harness() -> None:
    class UnknownHarness:
        value = "bogus"

    with pytest.raises(
        PassthroughError,
        match="Managed primary attach is not supported for bogus",
    ):
        get_passthrough(cast("Any", UnknownHarness()))


def test_codex_passthrough_builds_connection_config_with_ws_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "meridian.lib.harness.passthrough.codex._reserve_local_port",
        lambda host="127.0.0.1": 43123,
    )
    env = {"MERIDIAN_TEST": "1"}
    config = CodexPassthrough().build_config(
        spawn_id=SpawnId("p100"),
        spec=CodexLaunchSpec(
            prompt="hello codex",
            model="codex-test",
            permission_resolver=_permission_resolver(),
        ),
        execution_cwd=tmp_path,
        env=env,
    )

    env["MERIDIAN_TEST"] = "mutated"

    assert config.spawn_id == SpawnId("p100")
    assert config.harness_id == HarnessId.CODEX
    assert config.prompt == "hello codex"
    assert config.project_root == tmp_path
    assert config.env_overrides == {"MERIDIAN_TEST": "1"}
    assert config.ws_bind_host == "127.0.0.1"
    assert config.ws_port == 43123


def test_opencode_passthrough_builds_connection_config(tmp_path: Path) -> None:
    env = {"MERIDIAN_TEST": "1"}
    config = OpenCodePassthrough().build_config(
        spawn_id=SpawnId("p101"),
        spec=OpenCodeLaunchSpec(
            prompt="hello opencode",
            appended_system_prompt="system prompt",
            permission_resolver=_permission_resolver(),
        ),
        execution_cwd=tmp_path,
        env=env,
    )

    env["MERIDIAN_TEST"] = "mutated"

    assert config.spawn_id == SpawnId("p101")
    assert config.harness_id == HarnessId.OPENCODE
    assert config.prompt == "hello opencode"
    assert config.project_root == tmp_path
    assert config.env_overrides == {"MERIDIAN_TEST": "1"}
    assert config.system == "system prompt"
    assert config.ws_port == 0


def test_claude_passthrough_stub_raises_for_managed_primary_attach(tmp_path: Path) -> None:
    passthrough = ClaudePassthrough()

    with pytest.raises(
        PassthroughError,
        match="Managed primary attach is not supported for claude",
    ):
        passthrough.build_config(
            spawn_id=SpawnId("p102"),
            spec=ClaudeLaunchSpec(
                prompt="hello claude",
                permission_resolver=_permission_resolver(),
            ),
            execution_cwd=tmp_path,
            env={},
        )

    with pytest.raises(
        PassthroughError,
        match="Managed primary attach is not supported for claude",
    ):
        passthrough.build_tui_command(
            cast("Any", object()),
            ClaudeLaunchSpec(
                prompt="hello claude",
                permission_resolver=_permission_resolver(),
            ),
        )


def test_passthrough_module_chain_imports_without_circular_imports() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import meridian.lib.harness.passthrough as package; "
                "import meridian.lib.harness.passthrough.base; "
                "import meridian.lib.harness.passthrough.claude; "
                "import meridian.lib.harness.passthrough.codex; "
                "import meridian.lib.harness.passthrough.opencode; "
                "import meridian.lib.harness.passthrough.registry as registry; "
                "assert package.get_passthrough is registry.get_passthrough"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_codex_passthrough_builds_tui_command_from_ws_observer_endpoint() -> None:
    connection = cast(
        "Any",
        type(
            "FakeConnection",
            (),
            {
                "harness_id": HarnessId.CODEX,
                "observer_endpoint": ObserverEndpoint(
                    transport="ws",
                    url="ws://127.0.0.1:43123",
                    host="127.0.0.1",
                    port=43123,
                ),
            },
        )(),
    )

    command = CodexPassthrough().build_tui_command(
        connection,
        CodexLaunchSpec(
            prompt="hello codex",
            permission_resolver=_permission_resolver(),
            projected_roots=(Path("/tmp/root-a"), Path("/tmp/root b")),
        ),
    )("thread-123")

    assert command == (
        "codex",
        "resume",
        "thread-123",
        "--remote",
        "ws://127.0.0.1:43123",
        "--add-dir",
        "/tmp/root-a",
        "--add-dir",
        "/tmp/root b",
    )


def test_opencode_passthrough_builds_tui_command_from_http_observer_endpoint() -> None:
    connection = cast(
        "Any",
        type(
            "FakeConnection",
            (),
            {
                "harness_id": HarnessId.OPENCODE,
                "observer_endpoint": ObserverEndpoint(
                    transport="http",
                    url="http://127.0.0.1:8765",
                    host="127.0.0.1",
                    port=8765,
                ),
            },
        )(),
    )

    command = OpenCodePassthrough().build_tui_command(
        connection,
        OpenCodeLaunchSpec(
            prompt="hello opencode",
            permission_resolver=_permission_resolver(),
        ),
    )("session-456")

    assert command == ("opencode", "attach", "http://127.0.0.1:8765", "--session", "session-456")
