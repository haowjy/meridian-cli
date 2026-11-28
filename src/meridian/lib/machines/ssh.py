"""One ``ssh`` child with its output read by threads, so every wait has a deadline.

Meridian stops only the ``ssh`` it started, through the handle of that unreaped child.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import IO, Final

from meridian.lib.core.caller_boundary import SANDBOX_BLOCKED_SENTENCE, inside_launcher_sandbox
from meridian.lib.machines.results import NotSent

# herdr's set, plus -T and LogLevel=ERROR: -T overrides a user's `RequestTTY force`, and
# LogLevel=ERROR overrides `LogLevel QUIET`, which would hide whether a sign-in or a host key
# failed.
BACKGROUND_OPTIONS: Final = (
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "NumberOfPasswordPrompts=0",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ConnectionAttempts=1",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
    "-o",
    "LogLevel=ERROR",
)
_STDERR_CAP: Final = 16 * 1024
_EOF: Final = object()
TIMED_OUT: Final = object()


@dataclass(frozen=True)
class SshExit:
    status: int
    stderr: str

    @property
    def last_line(self) -> str:
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        return lines[-1] if lines else ""


class CappedReader:
    """Reads a stream on a thread, keeping the first *cap* bytes and draining the rest: a full
    pipe can't hold the child until its time limit."""

    def __init__(self, stream: IO[bytes], cap: int) -> None:
        self._stream = stream
        self._cap = cap
        self._kept = bytearray()
        self.truncated = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # os.read: whatever has arrived, so a grandchild that keeps the pipe open after the
        # child exits can't hold the bytes already written inside a read that waits for 64 KiB.
        fd = self._stream.fileno()
        while chunk := os.read(fd, 65536):
            room = self._cap - len(self._kept)
            if room > 0:
                self._kept.extend(chunk[:room])
            if len(chunk) > room:
                self.truncated = True

    def collect(self, timeout: float) -> bytes:
        """What was read, waiting up to *timeout* for the end of the stream: a grandchild that
        still holds the pipe doesn't hold this up."""

        self._thread.join(timeout)
        return bytes(self._kept)


class SshUnavailable(OSError):
    """``ssh`` itself can't be started."""


class SshProcess:
    def __init__(self, target: str, remote_words: tuple[str, ...]) -> None:
        try:
            self._process = subprocess.Popen(
                ["ssh", *BACKGROUND_OPTIONS, "--", target, *remote_words],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SshUnavailable(
                "`ssh` is not installed or not on PATH; Meridian reaches other machines through it"
            ) from exc
        assert self._process.stdout is not None and self._process.stderr is not None
        self._lines: queue.Queue[object] = queue.Queue()
        stdout, stderr = self._process.stdout, self._process.stderr
        threading.Thread(target=self._read_lines, args=(stdout,), daemon=True).start()
        self._stderr = CappedReader(stderr, _STDERR_CAP)

    def _read_lines(self, stream: IO[bytes]) -> None:
        try:
            for line in iter(stream.readline, b""):
                self._lines.put(line)
        finally:
            self._lines.put(_EOF)

    def read_line(self, deadline: float) -> bytes | None | object:
        """The next stdout line, ``None`` at end of output, ``TIMED_OUT`` at the deadline."""

        try:
            item = self._lines.get(timeout=max(deadline - time.monotonic(), 0.0))
        except queue.Empty:
            return TIMED_OUT
        if item is _EOF:
            self._lines.put(_EOF)
            return None
        assert isinstance(item, bytes)
        return item

    def write(self, data: bytes, deadline: float | None = None) -> None:
        """Write *data* to the other side's stdin; with *deadline*, a far side that is alive and
        not reading ends the call there (``TimeoutError``), not at the keepalive."""

        stdin = self._process.stdin
        assert stdin is not None
        failure: list[BaseException] = []

        def push() -> None:
            try:
                stdin.write(data)
                stdin.flush()
            except BaseException as exc:
                failure.append(exc)

        writer = threading.Thread(target=push, daemon=True)
        writer.start()
        writer.join(None if deadline is None else max(deadline - time.monotonic(), 0.0))
        if writer.is_alive():
            self._process.kill()
            raise TimeoutError("the other side stopped reading the request")
        if failure:
            raise failure[0]

    def close_stdin(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            with suppress(OSError):
                stdin.close()

    def finish(self, grace: float = 5.0) -> SshExit:
        """Wait for ``ssh`` to exit (stopping it after *grace* seconds) and return its status."""

        self.close_stdin()
        try:
            status = self._process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._process.kill()
            status = self._process.wait()
        return SshExit(status, self._stderr.collect(2.0).decode("utf-8", "replace"))

    def kill(self) -> SshExit:
        self._process.kill()
        return self.finish(grace=2.0)

    def reap(self) -> None:
        """Stop and collect the ``ssh`` this started if it is still running."""

        if self._process.poll() is None:
            self.kill()


def not_connected(exited: SshExit, label: str, target: str) -> NotSent:
    """The result for an ``ssh`` that exited 255, so nothing reached a command on the other
    side. The one place that names why: from what ``ssh`` printed, a host key before a refused
    sign-in, anything else unreachable; and all of them ``sandboxed`` when this process is
    inside its session's sandbox, which is then what blocked ``ssh``. *label* is the machine's
    name (or the target, before it has one)."""

    last_line = exited.last_line or "ssh exited 255"
    sandbox = inside_launcher_sandbox()
    if sandbox is True:
        return NotSent("sandboxed", f"{SANDBOX_BLOCKED_SENTENCE} ({last_line})")
    stderr = exited.stderr.lower()
    changed = "remote host identification has changed" in stderr
    if changed or "host key verification failed" in stderr:
        kind = "host_key"
        text = (
            f"background calls accept only known host keys. If {label} is new to this machine, "
            f"run `meridian machine add {target}` in a terminal. If its key changed, fix "
            f"~/.ssh/known_hosts yourself; Meridian never does. ({last_line})"
        )
    elif "permission denied" in stderr:
        kind = "auth"
        text = (
            f"sign-in to {label} failed; run `ssh {target}` once in a terminal, or load the key "
            f"with `ssh-add`. ({last_line})"
        )
    else:
        kind = "unreachable"
        text = f"{label} can't be reached over SSH: {last_line}"
    if sandbox is None:
        # A constrained sandbox was recorded and nothing says whether this command left it (macOS).
        text += f"; {SANDBOX_BLOCKED_SENTENCE}"
    return NotSent(kind, text)


__all__ = [
    "BACKGROUND_OPTIONS",
    "TIMED_OUT",
    "CappedReader",
    "SshExit",
    "SshProcess",
    "SshUnavailable",
    "not_connected",
]
