"""Exact spawn liveness from the runner-lifetime launch lock."""

from __future__ import annotations

import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

import meridian.lib.state.spawn_status as spawn_status
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.state import spawn_store
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, record_to_stored_state
from meridian.lib.state.spawn.transitions import apply_finalize
from meridian.lib.state.spawn_status import CRASHED, overlay_derived, read_derived
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest

pytestmark = posix_only

HOLDER = """
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CLOEXEC)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.buffer.read()
"""

RACE_WRITER = """
import fcntl
import os
import sys
from pathlib import Path

lock_path, state_path, active_path, terminal_path = map(Path, sys.argv[1:])
active = active_path.read_bytes()
terminal = terminal_path.read_bytes()
fd = None
sequence = 0

def publish(payload):
    global sequence
    sequence += 1
    temporary = state_path.with_name(f".race-{os.getpid()}-{sequence}")
    temporary.write_bytes(payload)
    os.replace(temporary, state_path)

for command in sys.stdin:
    if command.strip() == "try_active":
        candidate = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(candidate)
            print("blocked", flush=True)
        else:
            publish(active)
            os.close(candidate)
            print("published", flush=True)
    elif command.strip() == "acquire_active":
        fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
        fcntl.flock(fd, fcntl.LOCK_EX)
        publish(active)
        print("held", flush=True)
    elif command.strip() == "terminal_release":
        publish(terminal)
        os.close(fd)
        fd = None
        print("released", flush=True)
"""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _seed(
    runtime_root: Path,
    spawn_id: str,
    *,
    status: SpawnStatus = SpawnStatus.RUNNING,
    started_at: str | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
) -> SpawnRecord:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=f"c{spawn_id[1:]}",
        model="test",
        agent="",
        harness="claude",
        prompt="test",
        status=status,
        started_at=started_at or _iso_now(),
        runner_pid=runner_pid,
        runner_created_at_epoch=runner_created_at_epoch,
    )
    row = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    assert row is not None
    return row


def _publish(path: Path, row: SpawnRecord) -> None:
    stored = record_to_stored_state(row)
    atomic_write_text(path, stored.model_dump_json(indent=2) + "\n")


@contextmanager
def _holder(lock_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"held\n"
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def _race_command(process: subprocess.Popen[str], command: str) -> None:
    assert process.stdin is not None
    process.stdin.write(command + "\n")
    process.stdin.flush()


def _race_reply(process: subprocess.Popen[str], expected: str) -> None:
    assert process.stdout is not None
    assert process.stdout.readline().strip() == expected


def test_read_derived_races(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    active = _seed(runtime_root, "p1")
    terminal = apply_finalize(
        active,
        "succeeded",
        0,
        origin="runner",
        finished_at=_iso_now(),
        published_at=_iso_now(),
    )
    spawn_dir = runtime_root / "spawns" / "p1"
    lock_path = spawn_dir / "launch.lock"
    lock_path.touch()
    active_path = tmp_path / "active.json"
    terminal_path = tmp_path / "terminal.json"
    _publish(active_path, active)
    _publish(terminal_path, terminal)
    _publish(spawn_dir / "state.json", terminal)

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            RACE_WRITER,
            str(lock_path),
            str(spawn_dir / "state.json"),
            str(active_path),
            str(terminal_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    original_read_state = spawn_status.read_state
    phase: str | None = "free_to_active"

    def interleaving_read_state(
        spawns_dir: Path,
        spawn_id: str,
        *,
        include_prompt: bool = True,
    ) -> SpawnRecord | None:
        if phase == "free_to_active":
            _race_command(process, "try_active")
            _race_reply(process, "blocked")
        row = original_read_state(
            spawns_dir,
            spawn_id,
            include_prompt=include_prompt,
        )
        if phase == "active_to_terminal":
            _race_command(process, "terminal_release")
            _race_reply(process, "released")
        return row

    monkeypatch.setattr(spawn_status, "read_state", interleaving_read_state)
    try:
        # The lock is free with a terminal row, then a new holder acquires it
        # and publishes active state inside the read seam. The real shared hold
        # blocks that writer, while a lock-then-read split invents a crash.
        first = spawn_status.read_derived(runtime_root, "p1")
        assert first is not None and first.status == "succeeded"

        phase = None
        _race_command(process, "acquire_active")
        _race_reply(process, "held")

        # The holder publishes its terminal outcome and releases just after
        # the row read. A read-then-lock split invents a crash from those facts.
        phase = "active_to_terminal"
        second = spawn_status.read_derived(runtime_root, "p1")
        assert second is not None and second.status == "running"
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=5)


def test_read_derived_tracks_held_and_released_lock(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _seed(runtime_root, "p1", status=SpawnStatus.FINALIZING)
    lock_path = runtime_root / "spawns" / "p1" / "launch.lock"
    lock_path.touch()

    with _holder(lock_path) as holder:
        live = read_derived(runtime_root, "p1")
        assert live is not None
        assert live.status == "finalizing"
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5)

        crashed = read_derived(runtime_root, "p1")
        assert crashed is not None
        assert crashed.status == "failed"
        assert crashed.terminal is not None
        assert crashed.terminal.error == CRASHED


def test_read_derived_uses_cancel_intent(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _seed(runtime_root, "p1", status=SpawnStatus.FINALIZING)
    (runtime_root / "spawns" / "p1" / "launch.lock").touch()

    spawn_store.record_cancel_intent(
        runtime_root,
        "p1",
        exit_code=130,
        error="cancelled by user",
        requested_at=_iso_now(),
    )
    cancelled = read_derived(runtime_root, "p1")
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.terminal is not None
    assert cancelled.terminal.error == "cancelled by user"


def test_overlay_derived_preserves_vanished_active_row(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    stored = _seed(runtime_root, "p1", status=SpawnStatus.FINALIZING)
    vanished = stored.model_copy(update={"id": "p2"})
    assert overlay_derived(runtime_root, [vanished]) == [vanished]


def test_read_derived_legacy_runner_rules(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        birth = psutil.Process(sleeper.pid).create_time()
        _seed(
            runtime_root,
            "p1",
            runner_pid=sleeper.pid,
            runner_created_at_epoch=birth,
        )
        _seed(runtime_root, "p2", status=SpawnStatus.QUEUED)
        _seed(
            runtime_root,
            "p3",
            started_at=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        )

        live_runner = read_derived(runtime_root, "p1")
        startup_grace = read_derived(runtime_root, "p2")
        expired_grace = read_derived(runtime_root, "p3")
        assert live_runner is not None and live_runner.status == "running"
        assert startup_grace is not None and startup_grace.status == "queued"
        assert expired_grace is not None and expired_grace.status == "failed"
        assert expired_grace.terminal is not None
        assert expired_grace.terminal.error == CRASHED

        sleeper.kill()
        sleeper.wait(timeout=5)

        dead_runner = read_derived(runtime_root, "p1")
        assert dead_runner is not None and dead_runner.status == "failed"
        assert dead_runner.terminal is not None
        assert dead_runner.terminal.error == CRASHED
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=5)


def test_read_derived_tolerates_legacy_started_at(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    naive_utc = (datetime.now(UTC) - timedelta(seconds=30)).replace(tzinfo=None).isoformat()
    _seed(runtime_root, "p1", started_at=naive_utc)
    _seed(runtime_root, "p2", started_at="not-a-timestamp")

    naive = read_derived(runtime_root, "p1")
    malformed = read_derived(runtime_root, "p2")

    assert naive is not None and naive.status == "failed"
    assert malformed is not None and malformed.status == "failed"
