from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import HarnessConnection, ObserverEndpoint
from meridian.lib.harness.passthrough.codex import CodexPassthrough
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class FakeCodexConnection:
    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def observer_endpoint(self) -> ObserverEndpoint:
        return ObserverEndpoint(
            transport="ws",
            url="ws://127.0.0.1:4545",
            host="127.0.0.1",
            port=4545,
        )


def test_codex_primary_attach_command_forwards_initial_prompt(tmp_path: Path) -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        prompt="prior context\n\nactual task",
        user_turn_content="prior context\n\nactual task",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        interactive=True,
        projected_roots=(tmp_path / "task",),
    )
    connection = cast("HarnessConnection[Any]", FakeCodexConnection())

    command = CodexPassthrough().build_tui_command(connection, spec)("thread-123")

    assert command[:4] == ("codex", "resume", "thread-123", "--remote")
    assert command[4] == "ws://127.0.0.1:4545"
    assert command[-3:] == ("--add-dir", (tmp_path / "task").as_posix(), spec.prompt)


def test_codex_primary_attach_command_omits_prompt_without_user_turn() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        prompt="synthetic bootstrap prompt",
        user_turn_content=None,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        interactive=True,
    )
    connection = cast("HarnessConnection[Any]", FakeCodexConnection())

    command = CodexPassthrough().build_tui_command(connection, spec)("thread-123")

    assert command == ("codex", "resume", "thread-123", "--remote", "ws://127.0.0.1:4545")
