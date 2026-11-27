"""One ``ssh`` child with its output read by threads, so every wait has a deadline.

Meridian stops only the ``ssh`` it started, through the handle of that unreaped child.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import IO, Final

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
        while chunk := self._stream.read(65536):
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


class SshProcess:
    def __init__(self, target: str, remote_words: tuple[str, ...]) -> None:
        self._process = subprocess.Popen(
            ["ssh", *BACKGROUND_OPTIONS, "--", target, *remote_words],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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

    def write(self, data: bytes) -> None:
        stdin = self._process.stdin
        assert stdin is not None
        stdin.write(data)
        stdin.flush()

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


def failure_text(kind: str, label: str, target: str, last_line: str) -> str:
    """What to tell the user when no ready line came: *label* is the machine's name (or the
    target, before it has one)."""

    if kind == "host_key":
        return (
            f"background calls accept only known host keys. If {label} is new to this machine, "
            f"run `meridian machine add {target}` in a terminal. If its key changed, fix "
            f"~/.ssh/known_hosts yourself; Meridian never does. ({last_line})"
        )
    if kind == "auth":
        return (
            f"sign-in to {label} failed; run `ssh {target}` once in a terminal, or load the key "
            f"with `ssh-add`. ({last_line})"
        )
    return f"{label} can't be reached over SSH: {last_line or 'no answer'}"


def failure_kind(stderr: str) -> str:
    """The error kind for a call that never reached a ready line, from what ``ssh`` printed:
    a host key before a refused sign-in, anything else unreachable."""

    text = stderr.lower()
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return "host_key"
    if "permission denied" in text:
        return "auth"
    return "unreachable"


__all__ = [
    "BACKGROUND_OPTIONS",
    "TIMED_OUT",
    "CappedReader",
    "SshExit",
    "SshProcess",
    "failure_kind",
    "failure_text",
]
