"""Subprocess-backed process launching."""

from __future__ import annotations

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
    wait_cancelled: threading.Event,
) -> None:
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
            _write_chunk_to_stderr(chunk)


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
        if self.output_log_path is None:
            try:
                if self.stderr_log_path is not None and self.process.stderr is not None:
                    stderr_thread = threading.Thread(
                        target=_read_pipe_to_stderr_log,
                        args=(
                            self.process.stderr,
                            self.stderr_log_path,
                            self.wait_cancelled,
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
    ) -> _RunningSubprocess:
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
                start_new_session=not IS_WINDOWS,
            )
        return _RunningSubprocess(
            process=process,
            output_log_path=output_log_path,
            stderr_log_path=stderr_log_path,
        )


__all__ = ["SubprocessProcessLauncher"]
