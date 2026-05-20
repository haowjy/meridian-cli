"""Subprocess-backed process launching."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from .ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    ChildStartedHook,
    LaunchedProcess,
    ProcessLauncher,
)


def _write_chunk_to_stdout(chunk: bytes) -> None:
    """Best-effort mirror of captured subprocess output to parent stdout."""

    try:
        stdout_buffer = getattr(sys.stdout, "buffer", None)
        if stdout_buffer is not None:
            stdout_buffer.write(chunk)
            stdout_buffer.flush()
            return
        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError):
        return


def _write_chunk_to_stderr(chunk: bytes) -> None:
    """Best-effort mirror of captured subprocess stderr to parent stderr."""

    try:
        stderr_buffer = getattr(sys.stderr, "buffer", None)
        if stderr_buffer is not None:
            stderr_buffer.write(chunk)
            stderr_buffer.flush()
            return
        sys.stderr.write(chunk.decode("utf-8", errors="replace"))
        sys.stderr.flush()
    except (BrokenPipeError, OSError, ValueError):
        return


def _read_pipe_to_stderr_log(
    stderr_stream: BinaryIO,
    stderr_log_path: Path,
) -> None:
    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
    with stderr_log_path.open("ab") as stderr_handle:
        while True:
            chunk = stderr_stream.read(4096)
            if not chunk:
                return
            stderr_handle.write(chunk)
            stderr_handle.flush()
            _write_chunk_to_stderr(chunk)


def _wait_for_process(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> int:
    try:
        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            if sys.platform == "win32":
                process.terminate()
            else:
                process.send_signal(signal.SIGINT)
            return process.wait()
        return 130


class SubprocessProcessLauncher(ProcessLauncher):
    """Portable subprocess launcher used when PTY capture is unavailable."""

    def launch(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: ChildStartedHook | None = None,
    ) -> LaunchedProcess:
        child_env = dict(env)
        stderr_log_path_raw = child_env.pop(PRIMARY_STDERR_LOG_PATH_ENV, "").strip()
        stderr_log_path = Path(stderr_log_path_raw) if stderr_log_path_raw else None

        if output_log_path is None:
            process: subprocess.Popen[str] | subprocess.Popen[bytes] = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                stderr=subprocess.PIPE if stderr_log_path is not None else None,
                text=stderr_log_path is None,
            )
        else:
            output_log_path.parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )
        if on_child_started is not None:
            try:
                on_child_started(process.pid)
            except Exception:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                raise
        stderr_thread: threading.Thread | None = None
        if output_log_path is None:
            try:
                if stderr_log_path is not None and process.stderr is not None:
                    stderr_thread = threading.Thread(
                        target=_read_pipe_to_stderr_log,
                        args=(process.stderr, stderr_log_path),
                        daemon=True,
                    )
                    stderr_thread.start()
                return LaunchedProcess(exit_code=_wait_for_process(process), pid=process.pid)
            finally:
                if stderr_thread is not None:
                    stderr_thread.join(timeout=1.0)
                with suppress(Exception):
                    if process.stderr is not None:
                        process.stderr.close()
        try:
            with output_log_path.open("wb") as output_handle:
                stdout_stream = process.stdout
                if stdout_stream is not None:
                    while True:
                        chunk = stdout_stream.read(4096)
                        if not chunk:
                            break
                        # text=False guarantees bytes; pyright sees union
                        chunk_bytes = chunk if isinstance(chunk, bytes) else chunk.encode()
                        output_handle.write(chunk_bytes)
                        output_handle.flush()
                        _write_chunk_to_stdout(chunk_bytes)
            return LaunchedProcess(exit_code=_wait_for_process(process), pid=process.pid)
        finally:
            with suppress(Exception):
                if process.stdout is not None:
                    process.stdout.close()


__all__ = ["SubprocessProcessLauncher"]
