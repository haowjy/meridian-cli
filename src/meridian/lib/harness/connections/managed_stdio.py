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
    StopResult,
    reap_on_ownership_transfer_failure,
)
from meridian.lib.harness.connections.errors import (
    IncompleteStartupTeardown,
    TeardownStatus,
)
from meridian.lib.harness.connections.managed_backend import (
    register_spawn_owned_process,
    spawn_owned_process_handle,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope import (
    CleanupResult,
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
)

STDIO_STDERR_TAIL_MAX_BYTES: Final[int] = 16 * 1024
_PROCESS_EXIT_CONFIRM_SECONDS: Final[float] = 1.0


def teardown_from_scope_cleanup(result: CleanupResult) -> TeardownStatus:
    """Reduce platform facts without teaching retry policy platform details."""

    if result.skip_reason is not None:
        return TeardownStatus.FAILED
    if not result.verification_complete or result.survivor_count is None:
        return TeardownStatus.UNKNOWN
    if result.survivor_count != 0:
        return TeardownStatus.FAILED
    return TeardownStatus.QUIESCENT


async def _terminate_scoped_process(
    process: asyncio.subprocess.Process,
    scope_handle: ScopedProcessHandle,
    *,
    grace_seconds: float,
    reason: str,
) -> StopResult:
    result = await scope_handle.terminate(
        grace_seconds=grace_seconds,
        reason=reason,
    )
    teardown = teardown_from_scope_cleanup(result)
    if teardown is TeardownStatus.QUIESCENT:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_EXIT_CONFIRM_SECONDS,
            )
        except (TimeoutError, asyncio.CancelledError):
            teardown = TeardownStatus.UNKNOWN
    return StopResult(escalated=result.kill_escalated, teardown=teardown)


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

    async def terminate(self) -> StopResult:
        process = self._process
        if process is None:
            return StopResult(teardown=TeardownStatus.QUIESCENT)

        scope_handle = self._scope_handle
        if scope_handle is not None:
            cleanup = await _terminate_scoped_process(
                process,
                scope_handle,
                grace_seconds=self._kill_grace_seconds,
                reason=self._terminate_reason,
            )
            if cleanup.teardown is TeardownStatus.QUIESCENT:
                self._scope_handle = None
                self._process = None
            return cleanup

        if process.returncode is not None:
            return StopResult(teardown=TeardownStatus.UNKNOWN)
        if process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()
        try:
            if IS_WINDOWS:
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=self._kill_grace_seconds)
        except TimeoutError:
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                return StopResult(escalated=True, teardown=TeardownStatus.FAILED)
        except (ProcessLookupError, OSError):
            return StopResult(teardown=TeardownStatus.FAILED)
        # Without the scope handle the root exit cannot prove descendants gone.
        return StopResult(teardown=TeardownStatus.UNKNOWN)

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
    process: asyncio.subprocess.Process | None = None
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
    except BaseException as exc:
        teardown = (
            TeardownStatus.QUIESCENT if process is None else TeardownStatus.UNKNOWN
        )
        if provisional_scope_handle is not None:
            async def _cleanup_provisional() -> TeardownStatus:
                cleanup = await _terminate_scoped_process(
                    provisional_scope_handle.process,
                    provisional_scope_handle,
                    grace_seconds=kill_grace_seconds,
                    reason=terminate_reason,
                )
                return cleanup.teardown

            teardown = await reap_on_ownership_transfer_failure(_cleanup_provisional)
        with suppress(OSError):
            stderr_handle.flush()
        stderr_handle.close()
        if (
            isinstance(exc, Exception)
            and not isinstance(exc, IncompleteStartupTeardown)
            and teardown is not TeardownStatus.QUIESCENT
        ):
            raise IncompleteStartupTeardown(exc, teardown=teardown) from exc
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
    "teardown_from_scope_cleanup",
]
