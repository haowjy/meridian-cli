"""OpenCode 2.x harness connection (session API + ``/api/event`` stream).

OpenCode 2 replaced the 1.x JSON routes with a versioned ``/api`` surface and
moved the terminal signal from ``session.idle`` to
``session.execution.succeeded`` / ``.failed`` / ``.interrupted``. The server also
requires basic auth (``opencode:<password>``), printing the password on stdout at
startup.

This transport subclasses the frozen 1.x connection to reuse its process
lifecycle, liveness policy, retry classification, and SSE framing. It overrides
the API surface, auth, payload shapes, resume model switch, and event-envelope
normalization. Verified against OpenCode 2.0.6.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar, cast

from meridian.lib.core.telemetry import StartupPhase
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import project_managed_primary_backend_command
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    RawHarnessEvent,
)
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.harness.connections.opencode_http import (
    OpenCodeV1Connection,
    SessionNotReadyError,
    _find_free_port,
    _materialize_system_prompt,
    _summarize_body,
)
from meridian.lib.harness.projections.project_opencode_streaming import (
    project_opencode_model,
    project_opencode_model_config,
    project_opencode_spec_to_session_payload,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.workspace_projection import OPENCODE_CONFIG_CONTENT_ENV
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
)

logger = logging.getLogger(__name__)

_SERVER_PASSWORD_RE = re.compile(r"server password (\S+)")


def _v2_session_id(body: object | None) -> str | None:
    """Read a session id from the V2 ``{data: {...}}`` response envelope."""

    if not isinstance(body, Mapping):
        return None
    mapping = cast("Mapping[str, object]", body)
    data = mapping.get("data")
    candidates: list[Mapping[str, object]] = []
    if isinstance(data, Mapping):
        candidates.append(cast("Mapping[str, object]", data))
    candidates.append(mapping)
    for candidate in candidates:
        value = candidate.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _drain_stream(stream: asyncio.StreamReader) -> None:
    """Keep the child's stdout pipe from filling after the handshake."""

    try:
        while await stream.read(4096):
            pass
    except asyncio.CancelledError:
        raise
    except Exception:  # pipe closed during teardown
        return


class OpenCodeV2Connection(OpenCodeV1Connection):
    """Bidirectional OpenCode 2.x connection over the ``/api`` server surface."""

    _CAPABILITIES: ClassVar[ConnectionCapabilities] = replace(
        OpenCodeV1Connection._CAPABILITIES,
        runtime_model_switch=True,
    )
    _HEALTH_PATHS: ClassVar[tuple[str, ...]] = ("/api/info",)
    _CREATE_SESSION_PATH: ClassVar[str] = "/api/session"
    _MESSAGE_PATH_TEMPLATES: ClassVar[tuple[str, ...]] = (
        "/api/session/{session_id}/prompt",
    )
    _EVENT_PATHS: ClassVar[tuple[str, ...]] = ("/api/event",)
    _CANCEL_PATH_TEMPLATES: ClassVar[tuple[str, ...]] = (
        "/api/session/{session_id}/interrupt",
    )

    def __init__(self) -> None:
        super().__init__()
        self._server_password: str | None = None
        self._stdout_drain_task: asyncio.Task[None] | None = None

    async def _launch_process(
        self, config: ConnectionConfig, spec: ResolvedLaunchSpec
    ) -> None:
        host = config.ws_bind_host or "127.0.0.1"
        port = config.ws_port if config.ws_port > 0 else _find_free_port(host)
        self._base_url = f"http://{host}:{port}"
        command = project_managed_primary_backend_command(
            self.harness_id,
            spec,
            host=host,
            port=port,
        )
        env = dict(config.child_env)
        if spec.model and not spec.continue_session_id:
            env[OPENCODE_CONFIG_CONTENT_ENV] = project_opencode_model_config(
                env.get(OPENCODE_CONFIG_CONTENT_ENV), spec.model, self._model_agent_override
            )
        runtime_root = config.runtime_root or resolve_project_runtime_root_for_write(
            config.control_root
        )
        spawn_dir = resolve_spawn_log_dir(
            config.control_root, config.spawn_id, runtime_root=runtime_root
        )
        self._instruction_path = _materialize_system_prompt(config.system, env)
        self._stderr_log_path = spawn_dir / "stderr.log"
        self._stderr_handle = self._stderr_log_path.open("ab")
        self._stderr_read_offset = self._stderr_handle.tell()
        handle = await launch_managed_backend(
            ManagedBackendConfig(
                spawn_id=config.spawn_id,
                harness_id=self.harness_id,
                command=tuple(command),
                cwd=config.control_root,
                env=env,
                control_root=config.control_root,
            ),
            stderr=self._stderr_handle,
            stdout=asyncio.subprocess.PIPE,
        )
        self._process = handle.process
        self._scope_handle = handle.scope_handle
        self._parent_death_link = handle.parent_death_link

    async def _wait_for_ready(self, *, timeout_seconds: float) -> None:
        await self._await_server_password(timeout_seconds)
        await super()._wait_for_ready(timeout_seconds=timeout_seconds)

    async def _await_server_password(self, timeout_seconds: float) -> None:
        if self._server_password:
            return
        process = self._process
        stream = getattr(process, "stdout", None)
        if stream is None:
            raise RuntimeError("OpenCode V2 server stdout was not captured")
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        while True:
            if self._process_exited():
                raise self._startup_exit_exception()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "OpenCode V2 server did not print its password within the readiness budget"
                )
            try:
                line = await asyncio.wait_for(stream.readline(), timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError(
                    "OpenCode V2 server did not print its password within the readiness budget"
                ) from exc
            if not line:
                if self._process_exited():
                    raise self._startup_exit_exception()
                raise RuntimeError(
                    "OpenCode V2 server closed stdout before printing its password"
                )
            match = _SERVER_PASSWORD_RE.search(line.decode("utf-8", errors="replace"))
            if match:
                self._server_password = match.group(1)
                self._stdout_drain_task = asyncio.create_task(_drain_stream(stream))
                return

    async def _ensure_http_client(self) -> Any:
        if self._client is not None:
            return self._client
        aiohttp = self._ensure_aiohttp()
        timeout = aiohttp.ClientTimeout(total=None)
        self._client = aiohttp.ClientSession(
            timeout=timeout,
            auth=aiohttp.BasicAuth("opencode", self._server_password or ""),
        )
        return self._client

    async def _inspect_selected_model(
        self, model: str, *, expected_agent: str | None = None
    ) -> str | None:
        # V2 resolves the model from the session (set at create or via /model);
        # the 1.x native-agent config override does not apply here.
        _ = model, expected_agent
        return None

    async def _create_session(self, spec: ResolvedLaunchSpec) -> str:
        self._emit_startup_phase(StartupPhase.INITIALIZING_SESSION)

        if spec.continue_fork:
            raise HarnessCapabilityMismatch(
                "OpenCode V2 cannot express continue_fork over the session API."
            )

        continue_session_id = (spec.continue_session_id or "").strip()
        if continue_session_id:
            status, body, _ = await self._get_json(f"/api/session/{continue_session_id}")
            if status in self._SUCCESS_STATUSES:
                session_id = _v2_session_id(body)
                if session_id and session_id.strip() == continue_session_id:
                    if spec.model:
                        await self._switch_session_model(continue_session_id, spec.model)
                    return continue_session_id
                raise RuntimeError(
                    "OpenCode V2 session resume: GET returned mismatched id "
                    f"(expected={continue_session_id}, got={session_id})"
                )
            if status in self._PATH_RETRY_STATUSES:
                raise SessionNotReadyError(
                    f"OpenCode V2 session resume: session {continue_session_id} not yet "
                    f"loaded (status={status})"
                )
            raise RuntimeError(f"OpenCode V2 session resume: GET failed with status={status}")

        payload = project_opencode_spec_to_session_payload(spec)
        status, body, _ = await self._post_json(self._CREATE_SESSION_PATH, payload)
        if status in self._SUCCESS_STATUSES:
            session_id = _v2_session_id(body)
            if session_id is None:
                raise RuntimeError(
                    "OpenCode V2 session creation response missing session id: "
                    f"{_summarize_body(body)}"
                )
            return session_id
        if status in self._PATH_RETRY_STATUSES:
            raise SessionNotReadyError(
                f"OpenCode V2 session endpoint unavailable: status={status}"
            )
        raise RuntimeError(
            f"OpenCode V2 session create rejected payload: status={status} "
            f"body={_summarize_body(body)}"
        )

    async def _switch_session_model(self, session_id: str, model: str) -> None:
        """Apply the resolved model to a resumed session.

        The server resets the variant to default when omitted, and unknown routes
        fall through to an SPA ``200 text/html`` response, so success requires a
        non-HTML content type.
        """

        projected = project_opencode_model(model, id_field="id")
        if projected is None:
            return
        status, body, content_type = await self._post_json(
            f"/api/session/{session_id}/model",
            {"model": projected},
            skip_body_on_statuses=self._SUCCESS_STATUSES,
        )
        if status in self._SUCCESS_STATUSES and "text/html" not in content_type:
            return
        raise HarnessCapabilityMismatch(
            f"OpenCode V2 session model switch failed: status={status} "
            f"body={_summarize_body(body)}"
        )

    async def _post_session_message(
        self,
        text: str,
        *,
        system: str | None = None,
        fresh: bool = False,
        model: str | None = None,
    ) -> None:
        _ = system, fresh, model
        await self._post_session_action(
            path_templates=self._MESSAGE_PATH_TEMPLATES,
            payload_variants=({"text": text},),
            accepted_statuses=self._SUCCESS_STATUSES,
        )

    async def send_cancel(self) -> None:
        if self._cancel_requested:
            return
        if self._state in {"stopping", "stopped", "failed"}:
            self._cancel_requested = True
            return
        self._require_connected()
        self._cancel_requested = True
        self._signal_in_flight = True
        self._liveness.signal_request_in_flight("cancel")
        self._transition("stopping")
        await self._post_session_action(
            path_templates=self._CANCEL_PATH_TEMPLATES,
            payload_variants=({},),
            accepted_statuses=self._ACTION_SUCCESS_STATUSES,
        )

    def _event_from_json_line(
        self,
        json_text: str,
        *,
        raw_text: str,
        event_type_hint: str | None = None,
    ) -> RawHarnessEvent | None:
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed OpenCode V2 stream line: %s", raw_text)
            return None

        if not isinstance(parsed, dict):
            return RawHarnessEvent(
                event_type="unknown",
                payload={"value": cast("object", parsed)},
                harness_id=HarnessId.OPENCODE.value,
                raw_text=raw_text,
            )

        envelope = cast("dict[str, object]", parsed)
        data = envelope.get("data")
        flat: dict[str, object] = (
            dict(cast("Mapping[str, object]", data)) if isinstance(data, Mapping) else {}
        )
        durable = envelope.get("durable")
        if isinstance(durable, Mapping):
            aggregate = cast("Mapping[str, object]", durable).get("aggregateID")
            if isinstance(aggregate, str) and aggregate.strip():
                flat.setdefault("sessionID", aggregate)

        raw_type = envelope.get("type", event_type_hint or "unknown")
        event_type = raw_type if isinstance(raw_type, str) else "unknown"
        flat["type"] = event_type
        return RawHarnessEvent(
            event_type=event_type,
            payload=flat,
            harness_id=HarnessId.OPENCODE.value,
            raw_text=raw_text,
        )

    async def _cleanup_runtime(self, *, replacement_deadline: float | None = None) -> None:
        task = self._stdout_drain_task
        self._stdout_drain_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._server_password = None
        await super()._cleanup_runtime(replacement_deadline=replacement_deadline)


__all__ = ["OpenCodeV2Connection"]
