"""Subprocess-backed process launching."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from typing import BinaryIO

from meridian.lib.platform import IS_WINDOWS

from .ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    LaunchedProcess,
    ProcessLauncher,
    TerminalFds,
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


def _write_chunk_to_fd(fd: int, chunk: bytes) -> None:
    """Best-effort write of one captured chunk to a terminal descriptor."""

    view = memoryview(chunk)
    try:
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except (BrokenPipeError, OSError):
        return


def _read_pipe_to_stderr_log(
    stderr_stream: BinaryIO,
    stderr_log_path: Path,
    wait_cancelled: threading.Event,
    terminal_stderr_fd: int,
) -> None:
    try:
        stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        with stderr_log_path.open("ab") as stderr_handle:
            while True:
                chunk = stderr_stream.read(4096)
                if not chunk:
                    return
                if wait_cancelled.is_set():
                    return
                stderr_handle.write(chunk)
                stderr_handle.flush()
                _write_chunk_to_fd(terminal_stderr_fd, chunk)
    finally:
        with suppress(OSError):
            os.close(terminal_stderr_fd)


def _read_pipe_to_queue(
    stream: BinaryIO,
    chunks: Queue[bytes | None],
    wait_cancelled: threading.Event,
) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk or wait_cancelled.is_set():
                return
            while not wait_cancelled.is_set():
                try:
                    chunks.put(chunk, timeout=0.1)
                    break
                except Full:
                    continue
    finally:
        while not wait_cancelled.is_set():
            try:
                chunks.put(None, timeout=0.1)
                break
            except Full:
                continue


def _wait_for_process(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    *,
    wait_cancelled: threading.Event | None = None,
) -> int:
    while True:
        try:
            if wait_cancelled is None:
                return process.wait()
            return process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            if wait_cancelled is not None and wait_cancelled.is_set():
                return process.returncode if process.returncode is not None else 130
        except KeyboardInterrupt:
            if process.poll() is None:
                if IS_WINDOWS:
                    process.terminate()
                else:
                    process.send_signal(signal.SIGINT)
                return process.wait()
            return 130


@dataclass
class _RunningSubprocess:
    process: subprocess.Popen[str] | subprocess.Popen[bytes]
    output_log_path: Path | None
    stderr_log_path: Path | None
    terminal_stderr_fd: int | None
    wait_cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def pid(self) -> int:
        return self.process.pid

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()

    def cancel_wait(self) -> None:
        self.wait_cancelled.set()

    def wait(self) -> LaunchedProcess:
        stderr_thread: threading.Thread | None = None
        terminal_stderr_fd = self.terminal_stderr_fd
        self.terminal_stderr_fd = None
        if self.output_log_path is None:
            try:
                if (
                    self.stderr_log_path is not None
                    and self.process.stderr is not None
                    and terminal_stderr_fd is not None
                ):
                    stderr_thread = threading.Thread(
                        target=_read_pipe_to_stderr_log,
                        args=(
                            self.process.stderr,
                            self.stderr_log_path,
                            self.wait_cancelled,
                            terminal_stderr_fd,
                        ),
                        daemon=True,
                    )
                    stderr_thread.start()
                return LaunchedProcess(
                    exit_code=_wait_for_process(
                        self.process,
                        wait_cancelled=self.wait_cancelled,
                    ),
                    pid=self.process.pid,
                )
            finally:
                if stderr_thread is None and terminal_stderr_fd is not None:
                    with suppress(OSError):
                        os.close(terminal_stderr_fd)
                if stderr_thread is not None and not self.wait_cancelled.is_set():
                    stderr_thread.join(timeout=1.0)
                with suppress(Exception):
                    if (
                        self.process.stderr is not None
                        and (stderr_thread is None or not stderr_thread.is_alive())
                    ):
                        self.process.stderr.close()

        stdout_thread: threading.Thread | None = None
        try:
            with self.output_log_path.open("wb") as output_handle:
                stdout_stream = self.process.stdout
                if stdout_stream is not None:
                    chunks: Queue[bytes | None] = Queue(maxsize=16)
                    stdout_thread = threading.Thread(
                        target=_read_pipe_to_queue,
                        args=(stdout_stream, chunks, self.wait_cancelled),
                        daemon=True,
                    )
                    stdout_thread.start()
                    while True:
                        if self.wait_cancelled.is_set():
                            return LaunchedProcess(exit_code=130, pid=self.process.pid)
                        try:
                            chunk = chunks.get(timeout=0.1)
                        except Empty:
                            continue
                        if chunk is None:
                            break
                        output_handle.write(chunk)
                        output_handle.flush()
                        _write_chunk_to_stdout(chunk)
            return LaunchedProcess(
                exit_code=_wait_for_process(
                    self.process,
                    wait_cancelled=self.wait_cancelled,
                ),
                pid=self.process.pid,
            )
        finally:
            if stdout_thread is not None and not self.wait_cancelled.is_set():
                stdout_thread.join(timeout=1.0)
            with suppress(Exception):
                if (
                    self.process.stdout is not None
                    and (stdout_thread is None or not stdout_thread.is_alive())
                ):
                    self.process.stdout.close()


class SubprocessProcessLauncher(ProcessLauncher):
    """Portable subprocess launcher used when PTY capture is unavailable."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        terminal_fds: TerminalFds,
    ) -> _RunningSubprocess:
        child_env = dict(env)
        stderr_log_path_raw = child_env.pop(PRIMARY_STDERR_LOG_PATH_ENV, "").strip()
        stderr_log_path = Path(stderr_log_path_raw) if stderr_log_path_raw else None

        terminal_stderr_fd = (
            os.dup(terminal_fds.stderr)
            if output_log_path is None and stderr_log_path is not None
            else None
        )
        try:
            if output_log_path is None:
                process: subprocess.Popen[str] | subprocess.Popen[bytes] = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=child_env,
                    stdin=terminal_fds.stdin,
                    stdout=terminal_fds.stdout,
                    stderr=(
                        subprocess.PIPE
                        if stderr_log_path is not None
                        else terminal_fds.stderr
                    ),
                    text=stderr_log_path is None,
                )
            else:
                output_log_path.parent.mkdir(parents=True, exist_ok=True)
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=child_env,
                    stdin=terminal_fds.stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=False,
                    start_new_session=not IS_WINDOWS,
                )
        except BaseException:
            if terminal_stderr_fd is not None:
                with suppress(OSError):
                    os.close(terminal_stderr_fd)
            raise
        return _RunningSubprocess(
            process=process,
            output_log_path=output_log_path,
            stderr_log_path=stderr_log_path,
            terminal_stderr_fd=terminal_stderr_fd,
        )


__all__ = ["SubprocessProcessLauncher"]
