"""Cursor subprocess transport connection over stdout NDJSON."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from asyncio.subprocess import DEVNULL, PIPE, Process
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Final, cast

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    RawHarnessEvent,
    StopProgressCallback,
    StopResult,
    reap_on_ownership_transfer_failure,
    validate_prompt_size,
)
from meridian.lib.harness.connections.managed_stdio import (
    ManagedStdioProcess,
    launch_managed_stdio,
)
from meridian.lib.harness.extractors.cursor import CURSOR_EXTRACTOR
from meridian.lib.launch.constants import BASE_COMMAND_CURSOR_SUBPROCESS
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.platform.process_scope import ProcessScopeSnapshot

logger = logging.getLogger(__name__)

_HARNESS_NAME: Final[str] = HarnessId.CURSOR.value
_PROCESS_STOP_TIMEOUT_SECONDS: Final[float] = 10.0
_CREATE_CHAT_TIMEOUT_SECONDS: Final[float] = 30.0
_STDOUT_READLINE_LIMIT: Final[int] = 128 * 1024 * 1024  # 128 MiB — match claude_ws.py


class CursorSubprocessConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Subprocess Cursor harness connection over stdout NDJSON."""

    _CAPABILITIES = ConnectionCapabilities(
        # Literal["queue"|"interrupt_restart"|"http_post"] has no "none"; Cursor is
        # single-turn — send_user_message always raises, so injection is not usable.
        mid_turn_injection="queue",
        supports_steer=False,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=False,
        supports_primary_observer=False,
        supports_runtime_hitl=False,
    )

    def __init__(self) -> None:
        self._state: ConnectionState = "created"
        self._spawn_id: SpawnId = SpawnId("")
        self._session_id: str | None = None
        self._session_id_observer: Callable[[str], None] | None = None
        self._child: ManagedStdioProcess | None = None
        self._event_stream_started = False
        self._protocol_validated = False
        self._stop_lock = asyncio.Lock()

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CURSOR

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return self._CAPABILITIES

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        child = self._child
        return None if child is None else child.pid

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        child = self._child
        return None if child is None else child.scope_snapshot

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        if self._state != "created":
            raise RuntimeError(f"Cannot start Cursor connection from state '{self._state}'")

        validate_prompt_size(config)
        self._spawn_id = config.spawn_id
        self._set_state("starting")

        env = dict(config.child_env)
        self._session_id_observer = config.session_id_observer

        chat_id = (spec.continue_session_id or "").strip() or await _mint_chat_id(
            cwd=str(config.control_root),
            env=env,
        )
        if chat_id:
            self._session_id = chat_id
            if config.session_id_observer is not None:
                config.session_id_observer(chat_id)
            spec_for_cmd = spec.model_copy(update={"continue_session_id": chat_id})
        else:
            spec_for_cmd = spec

        command = project_subprocess_spec(
            self.harness_id,
            spec_for_cmd,
            base_command=BASE_COMMAND_CURSOR_SUBPROCESS,
        )

        try:
            self._child = await launch_managed_stdio(
                config=config,
                harness_id=self.harness_id,
                command=command,
                env=env,
                cwd=str(config.control_root),
                stdin=DEVNULL,
                stdout_limit=_STDOUT_READLINE_LIMIT,
                kill_grace_seconds=_PROCESS_STOP_TIMEOUT_SECONDS,
                terminate_reason="cursor_connection_stop",
            )
            self._set_state("connected")
        except BaseException:
            self._set_state("failed")
            await reap_on_ownership_transfer_failure(self._cleanup_start_failure)
            raise

    async def _cleanup_start_failure(self) -> None:
        async with self._stop_lock:
            await self._cleanup_resources(terminate_process=True)

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        async with self._stop_lock:
            if self._state == "stopped":
                return StopResult()
            if self._state not in {"stopping", "failed"}:
                self._set_state("stopping")
            await self._cleanup_resources(terminate_process=True)
            self._set_state("stopped")
            return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text
        raise RuntimeError(
            "Cursor subprocess transport does not support live user-message injection"
        )

    async def send_cancel(self) -> None:
        if self._state == "stopped":
            return
        if self._state != "failed":
            self._set_state("stopping")
        child = self._child
        if child is not None:
            await child.terminate()

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        child = self._child
        process = None if child is None else child.process
        if process is None:
            return
        stdout = process.stdout
        if stdout is None:
            return
        if self._event_stream_started:
            raise RuntimeError("events() iterator already consumed")
        self._event_stream_started = True

        while True:
            try:
                line_bytes = await stdout.readline()
            except Exception as exc:
                if self._state not in {"stopping", "stopped"}:
                    detail = f"Failed to read Cursor stdout: {exc}"
                    self._set_state("failed")
                    yield self._error_event(detail)
                return

            if not line_bytes:
                return_code = process.returncode
                if return_code is None:
                    return_code = await process.wait()
                if return_code != 0 and self._state not in {"stopping", "stopped"}:
                    detail = f"Cursor subprocess exited with code {return_code}."
                    self._set_state("failed")
                    yield self._error_event(detail)
                return

            raw_text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not raw_text.strip():
                continue
            event = self._parse_stdout_line(raw_text)
            if event is None:
                if self._protocol_validated:
                    continue
                detail = (
                    "Cursor protocol mismatch: expected NDJSON object with non-empty string "
                    f"'type', got: {raw_text.strip()!r}"
                )
                self._set_state("failed")
                yield self._error_event(detail, raw_text=raw_text)
                return

            self._protocol_validated = True
            self._observe_session_id_from_event(event)
            yield event

    def _parse_stdout_line(self, raw_text: str) -> RawHarnessEvent | None:
        payload_text = raw_text.strip()
        if not payload_text:
            return None
        try:
            payload_obj = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed Cursor stdout line: %s", payload_text)
            return None
        if not isinstance(payload_obj, dict):
            logger.warning("Skipping non-object Cursor stdout line: %s", payload_text)
            return None

        payload = cast("dict[str, object]", payload_obj)
        raw_event_type = payload.get("type")
        if not isinstance(raw_event_type, str) or not raw_event_type.strip():
            logger.warning(
                "Skipping Cursor stdout line without string 'type': %s",
                payload_text,
            )
            return None

        return RawHarnessEvent(
            event_type=raw_event_type,
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=raw_text,
        )

    def _set_state(self, next_state: ConnectionState) -> None:
        if self._state == next_state:
            return
        self._state = next_state

    async def _cleanup_resources(self, *, terminate_process: bool) -> None:
        child = self._child
        if child is None:
            return
        if terminate_process:
            await child.terminate()
        child.close_stderr_handle()

    def _observe_session_id_from_event(self, event: RawHarnessEvent) -> None:
        detected = CURSOR_EXTRACTOR.detect_session_id_from_event(event)
        if not detected:
            return
        self._session_id = detected
        if self._session_id_observer is not None:
            self._session_id_observer(detected)

    def _error_event(self, message: str, *, raw_text: str | None = None) -> RawHarnessEvent:
        return RawHarnessEvent(
            event_type="meridian/error/connectionClosed",
            payload={"type": "error", "message": message},
            harness_id=_HARNESS_NAME,
            raw_text=raw_text,
        )


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _mint_chat_id(*, cwd: str, env: dict[str, str]) -> str | None:
    """Mint a Cursor chat id via ``cursor agent create-chat``.

    Bounded by ``_CREATE_CHAT_TIMEOUT_SECONDS`` so a wedged ``create-chat`` cannot
    reintroduce an unbounded pre-launch hang. On any failure (timeout, nonzero exit,
    non-UUID output, exec error) it logs and degrades to ``None`` — the spawn then
    launches without a pre-recorded chat id (prior behavior).
    """
    process: Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            BASE_COMMAND_CURSOR_SUBPROCESS[0],
            "agent",
            "create-chat",
            cwd=cwd,
            env=env,
            stdin=DEVNULL,
            stdout=PIPE,
            stderr=DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=_CREATE_CHAT_TIMEOUT_SECONDS,
        )
        if process.returncode != 0:
            logger.warning(
                "cursor agent create-chat exited with code %s",
                process.returncode,
            )
            return None
        chat_id = stdout.decode("utf-8", errors="replace").strip()
        if not _looks_like_uuid(chat_id):
            logger.warning("cursor agent create-chat returned non-UUID: %r", chat_id)
            return None
        return chat_id
    except TimeoutError:
        logger.warning(
            "cursor agent create-chat timed out after %ss; launching without a minted chat id",
            _CREATE_CHAT_TIMEOUT_SECONDS,
        )
        if process is not None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.wait()
        return None
    except Exception:
        logger.warning("cursor agent create-chat failed", exc_info=True)
        return None


__all__ = ["CursorSubprocessConnection"]
