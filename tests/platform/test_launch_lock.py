"""#625: continue must sweep guardian-lost orphans before starting a harness."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.conftest import posix_only

pytestmark = posix_only
DRIVER = Path(__file__).parents[1] / "support" / "launch_lock_driver.py"


def until(check: Callable[[], Any], timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("bounded observation did not become true")


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def exited(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        # Linux can expose a zombie leader while other threads still own its
        # file table. That is not completed process exit (pidfd is not ready).
        return process.status() == psutil.STATUS_ZOMBIE and process.num_threads() == 1
    except psutil.NoSuchProcess:
        return True


class Runtime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n')
        binary = root / "claude"
        binary.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f"{shlex.quote(str(DRIVER))} harness \"$@\"\n"
        )
        binary.chmod(0o755)
        package = Path(__file__).parents[2]
        self.env = {
            "HOME": str(root / "user"), "USER": os.environ.get("USER", "test"),
            "PATH": str(root) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": f"{package}:{package / 'src'}",
            "FAKE_ROOT": str(root), "CLAUDE_CONFIG_DIR": str(root / "native"),
            "MERIDIAN_HOME": str(root / "home"), "_MERIDIAN_RUNTIME_DIR": str(root / "runtime"),
        }
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []

    def cli(self, *args: str) -> subprocess.Popen[bytes]:
        log = (self.root / f"cli-{len(self.processes)}.log").open("wb")
        self.logs.append(log)
        process = subprocess.Popen(
            [sys.executable, str(DRIVER), *args], cwd=self.repo, env=self.env,
            stdout=log, stderr=log, start_new_session=True,
        )
        self.processes.append(process)
        return process

    def pid(self, name: str) -> int:
        path = self.root / name
        until(lambda: path.exists() and path.read_text())
        return int(path.read_text())

    def row(self, spawn: str = "p1") -> dict[str, Any]:
        return json.loads((self.root / "runtime" / "spawns" / spawn / "state.json").read_text())

    def started(self, spawn: str = "p1") -> tuple[int, int]:
        return self.pid(f"{spawn}.pid"), self.pid(f"{spawn}.worker")

    def orphan(self, *, backend: bool = False) -> tuple[int, int]:
        if backend:
            self.env["FAKE_SHAPE"] = "backend"
        parent = self.cli("spawn", "-m", "haiku", "-p", "hold", "--bg")
        assert parent.wait(timeout=30) == 0
        harness, worker = self.started()
        scope = json.loads(
            (self.root / "runtime/spawns/p1/process_scopes.json").read_text()
        )["scopes"][0]
        pgid = scope["pgid"]
        guardians = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            with suppress(ProcessLookupError, psutil.NoSuchProcess):
                if os.getpgid(proc.pid) == pgid and "meridian-launch-gate" in " ".join(
                    proc.info["cmdline"] or []
                ):
                    guardians.append(proc.pid)
        assert guardians, "must kill the guardian first: otherwise PR 1 already cleans the group"
        for pid in guardians:
            os.kill(pid, signal.SIGKILL)
        runner = self.row()["runner_pid"]
        os.kill(runner, signal.SIGKILL)
        until(lambda: exited(runner))
        if backend:
            os.kill(harness, signal.SIGKILL)
            until(lambda: not alive(harness))
        assert alive(worker), "fixture must retain a guardian-lost orphan"
        return harness, worker

    def cleanup(self) -> None:
        for path in self.root.glob("*.pid"):
            with suppress(ProcessLookupError):
                os.killpg(int(path.read_text()), signal.SIGKILL)
        for path in self.root.glob("*.detached"):
            with suppress(ProcessLookupError):
                os.kill(int(path.read_text()), signal.SIGKILL)
        for state in (self.root / "runtime/spawns").glob("*/state.json"):
            pid = json.loads(state.read_text()).get("runner_pid")
            if pid:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        for log in self.logs:
            log.close()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    case = Runtime(tmp_path)
    try:
        yield case
    finally:
        case.cleanup()


def continue_orphan(runtime: Runtime, *, backend: bool) -> None:
    _, worker = runtime.orphan(backend=backend)
    process = runtime.cli(
        "spawn", "--continue", "p1", "-m", "haiku", "-p", "finish", "--no-background"
    )
    runtime.pid("p2.pid")
    orphan_gone_before_start = not alive(worker)
    (runtime.root / "p2.exit").touch()
    code = process.wait(timeout=30)
    assert (orphan_gone_before_start, code) == (True, 0), (
        "old group gone before new harness start, continue exit code",
        orphan_gone_before_start, code,
    )
    assert not (runtime.root / "p2.writer-busy").exists()


def test_continue_after_runner_sigkill_sweeps_orphan(runtime: Runtime) -> None:
    continue_orphan(runtime, backend=True)


def test_continue_after_runner_sigkill_sweeps_orphan_stdio(runtime: Runtime) -> None:
    continue_orphan(runtime, backend=False)


def test_continue_refuses_live_source(runtime: Runtime) -> None:
    parent = runtime.cli("spawn", "-m", "haiku", "-p", "hold", "--bg")
    assert parent.wait(timeout=30) == 0
    runtime.started()
    process = runtime.cli("spawn", "--continue", "p1", "-m", "haiku", "-p", "hold", "--bg")
    assert process.wait(timeout=15) != 0, "live source was not refused"
    assert not (runtime.root / "runtime/spawns/p2").exists(), "refusal wrote a new row"
    assert not (runtime.root / "p2.pid").exists()
    assert "still running" in (runtime.root / "cli-1.log").read_text()


def test_detached_session_descendant_survives_sweep(runtime: Runtime) -> None:
    runtime.env["FAKE_DETACHED"] = "1"
    _, worker = runtime.orphan()
    detached = runtime.pid("p1.detached")
    process = runtime.cli(
        "spawn", "--continue", "p1", "-m", "haiku", "-p", "finish", "--no-background"
    )
    runtime.started("p2")
    assert not alive(worker)
    assert alive(detached), "setsid tool is explicitly outside containment"
    (runtime.root / "p2.exit").touch()
    assert process.wait(timeout=30) == 0


@pytest.mark.xfail(strict=True, reason="PR 7 keeps the source lock across continue-in-place")
def test_concurrent_continue_of_dead_source(runtime: Runtime) -> None:
    runtime.orphan()
    # Neither follow-up gets to finish; both compete for the dead source.
    runtime.env["FAKE_RACE"] = "first"
    first = runtime.cli("spawn", "--continue", "p1", "-m", "haiku", "-p", "hold", "--bg")
    runtime.env["FAKE_RACE"] = "second"
    second = runtime.cli("spawn", "--continue", "p1", "-m", "haiku", "-p", "hold", "--bg")
    codes = [process.wait(timeout=30) for process in (first, second)]
    assert sorted(codes) == [0, 1]


def lock_held(path: Path) -> bool:
    with path.open("r") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


@pytest.mark.parametrize("shape", ["primary", "foreground", "background"])
def test_launch_lock_lifetime(runtime: Runtime, shape: str) -> None:
    args = ["-m", "haiku", "-p", "hold"]
    if shape != "primary":
        args = ["spawn", *args, "--bg" if shape == "background" else "--no-background"]
    parent = runtime.cli(*args)
    if shape == "background":
        assert parent.wait(timeout=30) == 0
    harness, worker = runtime.started()
    row = runtime.row()
    runner = row["runner_pid"]
    assert alive(runner)
    assert (runner != parent.pid) == (shape == "background")
    path = runtime.root / "runtime/spawns/p1/launch.lock"
    assert lock_held(path)
    if sys.platform == "linux":
        for pid in (harness, worker):
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                with suppress(FileNotFoundError):
                    assert os.readlink(fd) != str(path), "harness inherited the lifetime lock"
    (runtime.root / "p1.exit").touch()
    until(lambda: not lock_held(path))
    if shape != "background":
        assert parent.wait(timeout=30) == 0
    # A second launch is killed while its harness is still running.
    second = runtime.cli(*args)
    if shape == "background":
        assert second.wait(timeout=30) == 0
    runtime.started("p2")
    runner = runtime.row("p2")["runner_pid"]
    path = runtime.root / "runtime/spawns/p2/launch.lock"
    assert lock_held(path)
    os.kill(runner, signal.SIGKILL)
    if shape != "background":
        second.wait(timeout=10)
    else:
        until(lambda: exited(runner))
    assert not lock_held(path), "SIGKILL must release the lock without a grace period"


def test_continue_io_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from meridian.lib.launch.continue_sweep import sweep_before_continue
    from meridian.lib.state import spawn_store
    from meridian.lib.state.launch_lock import LaunchHandle
    from meridian.lib.state.spawn import repository
    from tests.support.call_probe import IOProbe

    root = tmp_path / "runtime"
    with LaunchHandle() as handle:
        spawn_store.start_spawn(
            root, spawn_id="p1", chat_id="c1", model="test", agent="", harness="claude",
            prompt="test", launch_lock=handle,
        )
    # Measure just the new seam. Pre-existing session discovery is not a lock/sweep cost.
    probe = IOProbe(monkeypatch)
    probe.watch("scan", repository.scan_spawn_ids)
    probe.watch("read", repository.read_state)
    for count in (240, 2400, 12000):
        for number in range(2, count + 1):
            (root / "spawns" / f"p{number}").mkdir(exist_ok=True)
        before = probe.count("read")
        sweep_before_continue(root, "p1")
        assert probe.count("scan") == 0
        assert probe.count("read") == before == 0
