"""OpenCode managed-primary passthrough attach command shapes (V1 vs V2)."""

from __future__ import annotations

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import ObserverEndpoint
from meridian.lib.harness.connections.opencode_http import OpenCodeV1Connection
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.passthrough.opencode import (
    OpenCodePassthrough,
    build_opencode_attach_command,
    build_opencode_server_attach_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


class _StubConnection:
    """Minimal observer-endpoint carrier for passthrough command building."""

    def __init__(self, endpoint: ObserverEndpoint | None) -> None:
        self.observer_endpoint = endpoint
        self.harness_id = HarnessId.OPENCODE


def test_v1_attach_command_shape_is_unchanged() -> None:
    assert build_opencode_attach_command("ses_1", "http://127.0.0.1:4000") == (
        "opencode",
        "attach",
        "http://127.0.0.1:4000",
        "--session",
        "ses_1",
    )


def test_v2_server_attach_command_shape_uses_bare_tui() -> None:
    assert build_opencode_server_attach_command("ses_1", "http://127.0.0.1:4000") == (
        "opencode",
        "--server",
        "http://127.0.0.1:4000",
        "--session",
        "ses_1",
    )


def test_passthrough_selects_attach_style_from_the_observer_endpoint() -> None:
    passthrough = OpenCodePassthrough()
    spec = _spec()

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


def test_v1_connection_endpoint_keeps_attach_style_and_no_client_env() -> None:
    connection = OpenCodeV1Connection()
    connection._base_url = "http://127.0.0.1:4400"
    connection._primary_observer_mode = True

    endpoint = connection.observer_endpoint
    assert endpoint is not None
    assert endpoint.attach_style == "attach"
    assert dict(endpoint.client_env) == {}


def test_v2_connection_endpoint_without_password_has_empty_client_env() -> None:
    connection = OpenCodeV2Connection()
    connection._base_url = "http://127.0.0.1:4500"
    connection._primary_observer_mode = True

    endpoint = connection.observer_endpoint
    assert endpoint is not None
    assert endpoint.client_env == {}
