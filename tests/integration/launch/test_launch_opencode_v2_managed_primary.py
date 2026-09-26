"""End-to-end managed-primary launch coverage for OpenCode V2.

The V2 transport is contract-tested in ``tests/contract/test_opencode_v2_session_api.py``
and the attach command shapes are unit-tested in
``tests/unit/harness/test_opencode_v2_passthrough.py``. What was missing is a test
that takes a V2 selection from project config through the *launch seam* end to end:
config projection -> version-dispatching connection -> V2 transport -> observer
endpoint -> passthrough attach command -> TUI child env.

This test drives the real ``run_primary_attach`` seam with the boundary stubbed:
project config is real, the connection dispatcher and passthrough are real, and a
fake V2 server stands in for the ``opencode`` binary (process + HTTP). No real
binary is required, so it runs in CI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import opencode_connection as opencode_connection_module
from meridian.lib.harness.connections import opencode_v2_http as opencode_v2_module
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    ManagedBackendHandle,
)
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.launch.process import runner as runner_module
from meridian.lib.platform.detached_process import ParentDeathLink
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.integration.launch.test_launch_process_opencode import _build_primary_launch_context
from tests.integration.launch.test_primary_attach import FakeProcessLauncher
from tests.support.launch import stub_bundle_request_and_resolve

_SERVER_PASSWORD = "s3cret-v2-password"
_V2_SESSION_ID = "ses_v2_e2e"


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="google/gemini-2.5-pro",
        harness=HarnessId.OPENCODE,
    )


class _FakeV2Process:
    """Minimal managed-backend process whose stdout prints the server password."""

    def __init__(self, pid: int, stdout: asyncio.StreamReader) -> None:
        self.pid = pid
        self.stdout = stdout
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakeV2ServerConnection(OpenCodeV2Connection):
    """V2 transport with only the binary + HTTP wire faked.

    Everything above the boundary stays real: password parsing from stdout,
    readiness probing, session creation through the ``/api`` surface, the
    ``server`` attach style, and the ``OPENCODE_PASSWORD`` observer env.
    """

    def __init__(self, *, server_password: str, session_id: str) -> None:
        super().__init__()
        self._fake_server_password = server_password
        self._fake_session_id = session_id

    async def _get_json(
        self,
        path: str,
        *,
        timeout: float | None = None,
    ) -> tuple[int, object | None, str]:
        _ = path, timeout
        return (200, {"data": {}}, "application/json")

    async def _post_json(
        self,
        path: str,
        payload: object,
        *,
        skip_body_on_statuses: frozenset[int] | None = None,
        tolerate_incomplete_body: bool = False,
    ) -> tuple[int, object | None, str]:
        _ = payload, skip_body_on_statuses, tolerate_incomplete_body
        if path == self._CREATE_SESSION_PATH:
            return (200, {"data": {"id": self._fake_session_id}}, "application/json")
        return (200, {}, "application/json")

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        # No live frames; stay open until the launcher stops the connection.
        if False:  # pragma: no cover - keeps this an async generator
            yield RawHarnessEvent(
                event_type="unreachable",
                payload={},
                harness_id=HarnessId.OPENCODE.value,
            )
        await asyncio.Event().wait()


def _install_fake_v2_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[_FakeV2ServerConnection], list[str], list[ManagedBackendConfig]]:
    """Swap the dispatcher's V2 class and backend launcher for fakes."""

    created: list[_FakeV2ServerConnection] = []
    v1_calls: list[str] = []
    backend_launches: list[ManagedBackendConfig] = []

    def _v2_factory(request_handler: object | None = None) -> _FakeV2ServerConnection:
        _ = request_handler
        connection = _FakeV2ServerConnection(
            server_password=_SERVER_PASSWORD,
            session_id=_V2_SESSION_ID,
        )
        created.append(connection)
        return connection

    def _v1_factory() -> object:
        v1_calls.append("v1")
        raise AssertionError("V1 transport must not be selected when config selects v2")

    async def _fake_launch_managed_backend(
        config: ManagedBackendConfig,
        *,
        stderr: object,
        stdout: object = None,
    ) -> Any:
        _ = stderr, stdout
        backend_launches.append(config)
        reader = asyncio.StreamReader()
        reader.feed_data(f"server password {_SERVER_PASSWORD}\n".encode())
        reader.feed_eof()
        return ManagedBackendHandle(
            process=cast("Any", _FakeV2Process(pid=4242, stdout=reader)),
            scope_handle=cast("Any", None),
            parent_death_link=ParentDeathLink(parent_death_linked=False),
        )

    monkeypatch.setattr(opencode_connection_module, "OpenCodeV2Connection", _v2_factory)
    monkeypatch.setattr(opencode_connection_module, "OpenCodeV1Connection", _v1_factory)
    monkeypatch.setattr(
        opencode_v2_module,
        "launch_managed_backend",
        _fake_launch_managed_backend,
    )
    return created, v1_calls, backend_launches


def test_project_config_v2_drives_managed_primary_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_HARNESS_OPENCODE_VERSION", raising=False)
    (tmp_path / "meridian.toml").write_text(
        '[project]\nid = "opencode-v2-managed-primary"\n\n[harness.opencode]\nversion = "v2"\n',
        encoding="utf-8",
    )

    launch_context, _registry = _build_primary_launch_context(
        project_root=tmp_path,
        harness_id=HarnessId.OPENCODE,
        model="google/gemini-2.5-pro",
    )
    child_env = dict(launch_context.binding.environment.final_env)
    assert child_env["MERIDIAN_HARNESS_OPENCODE_VERSION"] == "v2"

    created, v1_calls, backend_launches = _install_fake_v2_transport(monkeypatch)

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    spawn_id = SpawnId("p-v2-managed-primary")
    spawn_dir = runtime_root / "spawns" / str(spawn_id)
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir)

    captured_endpoints: list[Any] = []

    def _capture_observer_endpoint(_pid: int) -> None:
        # Fires after the managed backend is connected but before the TUI starts;
        # the endpoint is torn down when the launcher stops the connection.
        assert created, "V2 connection must exist before the TUI starts"
        captured_endpoints.append(created[0].observer_endpoint)

    outcome = runner_module.run_primary_attach(
        harness_id=HarnessId.OPENCODE,
        spawn_id=spawn_id,
        spawn_dir=spawn_dir,
        control_root=tmp_path,
        task_cwd=None,
        env=child_env,
        spec=launch_context.binding.spec,
        process_launcher=process_launcher,
        on_running=_capture_observer_endpoint,
    )

    # Version dispatch selected the V2 transport, not V1.
    assert v1_calls == []
    assert len(created) == 1
    assert len(captured_endpoints) == 1
    endpoint = captured_endpoints[0]
    assert endpoint is not None
    assert endpoint.url is not None

    # Observer endpoint wiring: V2 server style with the printed password.
    assert endpoint.transport == "http"
    assert endpoint.attach_style == "server"
    assert endpoint.client_env == {"OPENCODE_PASSWORD": _SERVER_PASSWORD}

    # The managed backend was launched as an `opencode` server for the V2 transport.
    assert len(backend_launches) == 1
    backend_command = backend_launches[0].command
    assert backend_command[0] == "opencode"
    assert "serve" in backend_command
    # The password is process-local to the attach client, never the backend's own env.
    assert "OPENCODE_PASSWORD" not in backend_launches[0].env

    # Attach command: bare V2 TUI pointed at the server for the created session.
    assert process_launcher.launch_commands == [
        (
            "opencode",
            "--server",
            cast("str", endpoint.url),
            "--session",
            _V2_SESSION_ID,
            "--prompt",
            "primary prompt",
        )
    ]
    assert process_launcher.launch_envs[0]["OPENCODE_PASSWORD"] == _SERVER_PASSWORD
    assert process_launcher.output_log_paths == [None]

    # Metadata reflects the managed V2 session and observer port.
    metadata = process_launcher.metadata_seen_at_launch
    assert metadata is not None
    assert metadata["backend_pid"] == 4242
    assert metadata["backend_port"] == endpoint.port
    assert metadata["harness_session_id"] == _V2_SESSION_ID
    assert outcome.exit_code == 0
    assert outcome.session_id == _V2_SESSION_ID
    assert outcome.tui_pid == process_launcher.pid
