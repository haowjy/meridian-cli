"""Managed stdio child process launch and cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from contextlib import suppress
from io import BufferedWriter
from pathlib import Path
from typing import Final

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    reap_on_ownership_transfer_failure,
)
from meridian.lib.harness.connections.managed_backend import (
    register_spawn_owned_process,
    spawn_owned_process_handle,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope import ProcessScopeSnapshot, ScopedProcessHandle
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
)

STDIO_STDERR_TAIL_MAX_BYTES: Final[int] = 16 * 1024


class ManagedStdioProcess:
    """Own a spawn-lifetime stdio child and its durable process scope."""

    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        scope_handle: ScopedProcessHandle,
        stderr_handle: BufferedWriter,
        stderr_log_path: Path,
        stderr_read_offset: int,
        kill_grace_seconds: float,
        terminate_reason: str,
    ) -> None:
        self._process: asyncio.subprocess.Process | None = process
        self._scope_handle: ScopedProcessHandle | None = scope_handle
        self._stderr_handle: BufferedWriter | None = stderr_handle
        self._stderr_log_path: Path | None = stderr_log_path
        self._stderr_read_offset = stderr_read_offset
        self._kill_grace_seconds = kill_grace_seconds
        self._terminate_reason = terminate_reason

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._process

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    @property
    def returncode(self) -> int | None:
        process = self._process
        return None if process is None else process.returncode

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        process = self._process
        return None if process is None else process.stdout

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        process = self._process
        return None if process is None else process.stdin

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        handle = self._scope_handle
        return None if handle is None else handle.snapshot

    @property
    def stderr_log_path(self) -> Path | None:
        return self._stderr_log_path

    async def wait_for_exit(self, *, timeout: float) -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def terminate(self) -> bool:
        process = self._process
        if process is None:
            return False

        scope_handle = self._scope_handle
        self._scope_handle = None
        if process.returncode is not None:
            self._process = None
            return False
        if scope_handle is not None:
            result = await scope_handle.terminate(
                grace_seconds=self._kill_grace_seconds,
                reason=self._terminate_reason,
            )
            self._process = None
            return result.kill_escalated

        if process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()
        if IS_WINDOWS:
            with suppress(ProcessLookupError):
                process.terminate()
        else:
            with suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._kill_grace_seconds)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        self._process = None
        return True

    def read_stderr_tail(
        self,
        *,
        max_bytes: int = STDIO_STDERR_TAIL_MAX_BYTES,
    ) -> str | None:
        handle = self._stderr_handle
        if handle is not None:
            with suppress(OSError):
                handle.flush()
        path = self._stderr_log_path
        if path is None or not path.is_file():
            return None
        try:
            with path.open("rb") as reader:
                end = reader.seek(0, os.SEEK_END)
                start = min(self._stderr_read_offset, end)
                read_from = max(start, end - max_bytes)
                reader.seek(read_from)
                raw = reader.read(end - read_from)
        except OSError:
            return None
        decoded = raw.decode("utf-8", errors="replace").strip()
        return decoded or None

    def close_stderr_handle(self) -> None:
        handle = self._stderr_handle
        if handle is None:
            return
        with suppress(OSError):
            handle.flush()
        handle.close()
        self._stderr_handle = None


async def launch_managed_stdio(
    *,
    config: ConnectionConfig,
    harness_id: HarnessId,
    command: Sequence[str],
    env: Mapping[str, str],
    cwd: str,
    stdin: int,
    stdout_limit: int,
    kill_grace_seconds: float,
    terminate_reason: str,
) -> ManagedStdioProcess:
    """Launch and register a spawn-lifetime stdio child process."""

    spawn_dir = resolve_spawn_log_dir(
        config.control_root,
        config.spawn_id,
        runtime_root=(
            config.runtime_root
            or resolve_project_runtime_root_for_write(config.control_root)
        ),
    )
    stderr_log_path = spawn_dir / "stderr.log"
    stderr_handle = stderr_log_path.open("ab")
    stderr_read_offset = stderr_handle.tell()
    provisional_scope_handle: ScopedProcessHandle | None = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_handle,
                limit=stdout_limit,
                start_new_session=not IS_WINDOWS,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=harness_id,
                error=exc,
                binary_name=command[0],
            ) from exc
        provisional_scope_handle = spawn_owned_process_handle(
            spawn_id=config.spawn_id,
            process=process,
            scope_id="stdio",
            role="harness_stdio",
        )
        scope_handle = await register_spawn_owned_process(
            spawn_id=config.spawn_id,
            control_root=config.control_root,
            process=process,
            scope_id="stdio",
            role="harness_stdio",
            runtime_root=config.runtime_root,
            persist=config.runtime_root is not None,
        )
    except BaseException:
        if provisional_scope_handle is not None:
            await reap_on_ownership_transfer_failure(
                lambda: provisional_scope_handle.terminate(
                    grace_seconds=kill_grace_seconds,
                    reason=terminate_reason,
                )
            )
        with suppress(OSError):
            stderr_handle.flush()
        stderr_handle.close()
        raise

    return ManagedStdioProcess(
        process=process,
        scope_handle=scope_handle,
        stderr_handle=stderr_handle,
        stderr_log_path=stderr_log_path,
        stderr_read_offset=stderr_read_offset,
        kill_grace_seconds=kill_grace_seconds,
        terminate_reason=terminate_reason,
    )


__all__ = [
    "STDIO_STDERR_TAIL_MAX_BYTES",
    "ManagedStdioProcess",
    "launch_managed_stdio",
]
