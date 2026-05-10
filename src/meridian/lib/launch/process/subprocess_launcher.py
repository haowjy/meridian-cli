"""Subprocess-backed process launching."""

from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from .ports import ChildStartedHook, LaunchedProcess, ProcessLauncher


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


def _wait_for_process(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> int:
    try:
        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
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
        if output_log_path is None:
            process: subprocess.Popen[str] | subprocess.Popen[bytes] = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                text=True,
            )
        else:
            output_log_path.parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
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
        if output_log_path is None:
            return LaunchedProcess(exit_code=_wait_for_process(process), pid=process.pid)
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
