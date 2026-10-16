"""OpenCode managed-primary passthrough attach command shapes (V1 vs V2)."""

from __future__ import annotations

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import ObserverEndpoint
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.passthrough.opencode import (
    OpenCodePassthrough,
    build_opencode_server_attach_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _spec(**overrides: object) -> ResolvedLaunchSpec:
    values: dict[str, object] = {
        "permission_resolver": UnsafeNoOpPermissionResolver(_suppress_warning=True),
    }
    values.update(overrides)
    return ResolvedLaunchSpec(**values)


class _StubConnection:
    """Minimal observer-endpoint carrier for passthrough command building."""

    def __init__(self, endpoint: ObserverEndpoint | None) -> None:
        self.observer_endpoint = endpoint
        self.harness_id = HarnessId.OPENCODE


@pytest.mark.parametrize("continuation", [False, True])
def test_server_attach_delivers_prompt_without_overwriting_session(continuation: bool) -> None:
    prompt = "Reply exactly: hello"
    session_id = "ses_1" if continuation else "ses_new"
    command = build_opencode_server_attach_command(session_id, "http://127.0.0.1:4000", prompt)

    assert command[-2:] == ("--prompt", prompt)
    assert command[command.index("--session") + 1] == session_id


def test_passthrough_selects_attach_style_from_the_observer_endpoint() -> None:
    passthrough = OpenCodePassthrough()
    spec = _spec(prompt="hello")

    v1_builder = passthrough.build_tui_command(
        _StubConnection(  # type: ignore[arg-type]
            ObserverEndpoint(transport="http", url="http://127.0.0.1:4100")
        ),
        spec,
    )
    assert v1_builder("ses_1") == (
        "opencode",
        "attach",
        "http://127.0.0.1:4100",
        "--session",
        "ses_1",
    )

    v2_builder = passthrough.build_tui_command(
        _StubConnection(  # type: ignore[arg-type]
            ObserverEndpoint(
                transport="http",
                url="http://127.0.0.1:4200",
                attach_style="server",
                client_env={"OPENCODE_PASSWORD": "secret"},
            )
        ),
        spec,
    )
    assert v2_builder("ses_2") == (
        "opencode",
        "--server",
        "http://127.0.0.1:4200",
        "--session",
        "ses_2",
        "--prompt",
        "hello",
    )


def test_v2_connection_endpoint_carries_password_and_server_style() -> None:
    connection = OpenCodeV2Connection()
    connection._base_url = "http://127.0.0.1:4300"
    connection._primary_observer_mode = True
    connection._server_password = "secret-token"

    endpoint = connection.observer_endpoint
    assert endpoint is not None
    assert endpoint.attach_style == "server"
    assert endpoint.client_env == {"OPENCODE_PASSWORD": "secret-token"}
