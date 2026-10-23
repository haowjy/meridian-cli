"""#625: continue must sweep guardian-lost orphans before starting a harness."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
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

from meridian.lib.state.session_store import chat_record
from meridian.lib.streaming.control_socket import socket_directory
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
            "FAKE_SOCKET_ROOT": str(socket_directory().parent),
            "FAKE_ROOT": str(root), "CLAUDE_CONFIG_DIR": str(root / "native"),
            "MERIDIAN_HOME": str(root / "home"), "_MERIDIAN_RUNTIME_DIR": str(root / "runtime"),
        }
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []
        self.groups: set[int] = set()

    def cli(self, *args: str) -> subprocess.Popen[bytes]:
        log = (self.root / f"cli-{len(self.processes)}.log").open("wb")
        self.logs.append(log)
        process = subprocess.Popen(
            [sys.executable, str(DRIVER), *args], cwd=self.repo, env=self.env,
            stdout=log, stderr=log, start_new_session=True,
        )
        self.processes.append(process)
        self.groups.add(process.pid)
        return process

    def pid(self, name: str) -> int:
        path = self.root / name
        until(lambda: path.exists() and path.read_text())
        pid = int(path.read_text())
        if name.endswith(".pid"):
            self.groups.add(pid)
        return pid

    def row(self, spawn: str = "p1") -> dict[str, Any]:
        return json.loads((self.root / "runtime" / "spawns" / spawn / "state.json").read_text())

    def started(self, spawn: str = "p1") -> tuple[int, int]:
        return self.pid(f"{spawn}.pid"), self.pid(f"{spawn}.worker")

    def orphan(self, *, backend: bool = False) -> tuple[int, int]:
        if backend:
            self.env["FAKE_SHAPE"] = "backend"
        parent = self.cli("spawn", "-m", "haiku", "-p", "hold", "--bg")
        assert parent.wait(timeout=30) == 0
        return self.orphan_spawn("p1", backend=backend)

    def orphan_spawn(self, spawn: str, *, backend: bool = False) -> tuple[int, int]:
        harness, worker = self.started(spawn)
        scope = json.loads(
            (self.root / f"runtime/spawns/{spawn}/process_scopes.json").read_text()
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
        runner = self.row(spawn)["runner_pid"]
        os.kill(runner, signal.SIGKILL)
        until(lambda: exited(runner))
        if backend:
            os.kill(harness, signal.SIGKILL)
            until(lambda: not alive(harness))
        assert alive(worker), "fixture must retain a guardian-lost orphan"
        return harness, worker

    def cleanup(self) -> None:
        self.groups.update(int(path.read_text()) for path in self.root.glob("*.pid"))
        for state in (self.root / "runtime/spawns").glob("*/state.json"):
            pid = json.loads(state.read_text()).get("runner_pid")
            if pid:
                self.groups.add(pid)
        for pgid in self.groups:
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
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


def continue_args(
    primary: bool, *, dry_run: bool = False, background: bool = False,
) -> list[str]:
    args = ["--continue", "p1", "-m", "haiku", "-p", "finish"]
    if not primary:
        args = ["spawn", *args, "--bg" if background else "--no-background"]
    if dry_run:
        args.append("--dry-run")
    return args


def continue_orphan(runtime: Runtime, *, backend: bool, primary: bool, dry_run: bool) -> None:
    _, worker = runtime.orphan(backend=backend)
    projection = runtime.root / "runtime/spawns/p1/process_scopes.json"
    before = projection.read_bytes()
    process = runtime.cli(*continue_args(primary, dry_run=dry_run))
    if dry_run:
        assert process.wait(timeout=30) == 0
        assert alive(worker), "dry run killed the orphan"
        assert projection.read_bytes() == before, "dry run released scopes"
        assert not (runtime.root / "runtime/spawns/p2").exists()
        return
    try:
        runtime.pid("p2.pid")
    except AssertionError as exc:
        log = runtime.root / f"cli-{runtime.processes.index(process)}.log"
        raise AssertionError(
            f"continue never started its harness; exit={process.poll()}\n"
            + log.read_text(errors="replace")[-4000:]
        ) from exc
    orphan_gone_before_start = not alive(worker)
    (runtime.root / "p2.exit").touch()
    code = process.wait(timeout=30)
    assert (orphan_gone_before_start, code) == (True, 0), (
        "old group gone before new harness start, continue exit code",
        orphan_gone_before_start, code,
    )
    assert not (runtime.root / "p2.writer-busy").exists()


@pytest.mark.parametrize("primary", [False, True], ids=["spawn", "primary"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
@pytest.mark.parametrize("backend", [False, True], ids=["stdio", "backend"])
def test_continue_after_runner_sigkill_sweeps_orphan(
    runtime: Runtime, primary: bool, dry_run: bool, backend: bool,
) -> None:
    continue_orphan(runtime, backend=backend, primary=primary, dry_run=dry_run)


@pytest.mark.parametrize("primary", [False, True], ids=["spawn", "primary"])
def test_continue_names_non_runner_holder_of_dead_source(
    runtime: Runtime, primary: bool,
) -> None:
    # Doctor (or another continue's sweep) may hold a dead source's lock while
    # it persists the crash. The refusal must not claim the dead source runs.
    runtime.orphan()
    lock = runtime.root / "runtime/spawns/p1/launch.lock"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR); "
            "fcntl.flock(fd,fcntl.LOCK_EX); print('held',flush=True); time.sleep(60)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == b"held\n"
        process = runtime.cli(*continue_args(primary))
        assert process.wait(timeout=30) != 0
        log = (runtime.root / f"cli-{runtime.processes.index(process)}.log").read_text()
        assert "held by a repair or another continue" in log
        assert "still running" not in log
        assert not (runtime.root / "runtime/spawns/p2").exists()
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("primary", [False, True], ids=["spawn", "primary"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
def test_continue_refuses_live_source(runtime: Runtime, primary: bool, dry_run: bool) -> None:
    parent = runtime.cli("spawn", "-m", "haiku", "-p", "hold", "--bg")
    assert parent.wait(timeout=30) == 0
    harness, worker = runtime.started()
    process = runtime.cli(*continue_args(primary, dry_run=dry_run))
    code = process.wait(timeout=30)
    assert code != 0
    assert alive(harness) and alive(worker)
    assert not (runtime.root / "runtime/spawns/p2").exists(), "preview/refusal wrote a new row"
    assert not (runtime.root / "p2.pid").exists()
    assert "still running" in (runtime.root / "cli-1.log").read_text()


@pytest.mark.parametrize("primary", [False, True], ids=["spawn", "primary"])
def test_continue_of_superseded_spawn_refuses_live_successor(
    runtime: Runtime, primary: bool,
) -> None:
    runtime.orphan()
    first = runtime.cli(*continue_args(False, background=True))
    assert first.wait(timeout=30) == 0
    p2_harness, p2_worker = runtime.started("p2")
    p2_scopes = runtime.root / "runtime/spawns/p2/process_scopes.json"
    scopes_before_refusal = p2_scopes.read_bytes()

    process = runtime.cli(*continue_args(primary, background=True))
    p3_row = runtime.root / "runtime/spawns/p3/state.json"
    until(lambda: process.poll() is not None or p3_row.exists())
    if p3_row.exists() and process.poll() is None:
        process.kill()
    code = process.wait(timeout=30)

    assert code != 0
    assert not p3_row.exists(), "refusal wrote a new row"
    assert alive(p2_harness) and alive(p2_worker)
    assert p2_scopes.read_bytes() == scopes_before_refusal, "refusal released live scopes"
    assert "p1 was continued as p2" in (runtime.root / "cli-2.log").read_text()


def test_background_continue_publishes_successor_before_parent_returns(
    runtime: Runtime,
) -> None:
    runtime.orphan()
    runtime.env["FAKE_HOLD_WORKER"] = "1"
    first = runtime.cli(*continue_args(False, background=True))
    runtime.pid("p2.worker-gate.pid")
    assert first.wait(timeout=30) == 0

    chat_id = runtime.row("p1")["chat_id"]
    chat = chat_record(runtime.root / "runtime", chat_id)
    assert chat is not None
    assert chat.spawn_id == "p2"

    (runtime.root / "p2.worker-release").touch()
    runtime.started("p2")


def test_continue_ignores_pruned_successor(runtime: Runtime) -> None:
    runtime.orphan()
    first = runtime.cli(*continue_args(False, background=True))
    assert first.wait(timeout=30) == 0
    p2_harness, p2_worker = runtime.started("p2")
    chat_id = runtime.row("p1")["chat_id"]
    chat = chat_record(runtime.root / "runtime", chat_id)
    assert chat is not None
    assert chat.spawn_id == "p2"
    p2_runner = runtime.row("p2")["runner_pid"]
    (runtime.root / "p2.exit").touch()
    # Prune only after p2's runner has finished writing its finalization.
    until(lambda: not alive(p2_harness) and not alive(p2_worker) and exited(p2_runner))
    shutil.rmtree(runtime.root / "runtime/spawns/p2")

    process = runtime.cli(*continue_args(False))
    p3_row = runtime.root / "runtime/spawns/p3/state.json"
    until(lambda: process.poll() is not None or p3_row.exists())
    if p3_row.exists():
        runtime.pid("p3.pid")
        (runtime.root / "p3.exit").touch()

    assert process.wait(timeout=30) == 0


def test_primary_continue_after_source_archive(runtime: Runtime) -> None:
    parent = runtime.cli("spawn", "-m", "haiku", "-p", "hold", "--bg")
    assert parent.wait(timeout=30) == 0
    runtime.started()
    (runtime.root / "p1.exit").touch()
    wait = runtime.cli("spawn", "wait", "p1")
    assert wait.wait(timeout=30) == 0

    archive = runtime.cli(
        "session", "archive", "c1", "--destination", str(runtime.root / "archive"), "--apply"
    )
    assert archive.wait(timeout=30) == 0
    assert not (runtime.root / "runtime/spawns/p1").exists()

    dry_run = runtime.cli("--continue", "c1", "--dry-run")
    assert dry_run.wait(timeout=30) == 0
    assert b" --resume " in (runtime.root / "cli-3.log").read_bytes()

    continued = runtime.cli("--continue", "c1")
    harness_marker = runtime.root / "p2.pid"
    until(lambda: continued.poll() is not None or harness_marker.exists())
    assert harness_marker.exists(), (runtime.root / "cli-4.log").read_text(errors="replace")
    runtime.started("p2")
    (runtime.root / "p2.exit").touch()
    assert continued.wait(timeout=30) == 0


def test_continue_of_superseded_spawn_sweeps_dead_successor(runtime: Runtime) -> None:
    runtime.orphan()
    first = runtime.cli(*continue_args(False, background=True))
    assert first.wait(timeout=30) == 0
    p2_harness, p2_worker = runtime.orphan_spawn("p2")

    process = runtime.cli(*continue_args(False))
    runtime.pid("p3.pid")
    successor_gone_before_start = not alive(p2_harness) and not alive(p2_worker)
    (runtime.root / "p3.exit").touch()

    assert process.wait(timeout=30) == 0
    assert successor_gone_before_start, "successor group survived until the new harness started"


def test_continue_refuses_live_legacy_runner(runtime: Runtime) -> None:
    root = runtime.root / "runtime"
    parent = runtime.cli("spawn", "-m", "haiku", "-p", "hold", "--no-background")
    runtime.started()
    # Remove the held lock's path to model an old-build active row. The runner
    # still holds the unlinked inode and supplies exact live-runner evidence.
    (root / "spawns/p1/launch.lock").unlink()
    process = runtime.cli(*continue_args(False))
    assert process.wait(timeout=30) != 0
    assert "running under an older meridian" in (runtime.root / "cli-1.log").read_text()
    assert not (root / "spawns/p1/launch.lock").exists()
    assert parent.poll() is None
    (runtime.root / "p1.exit").touch()
    assert parent.wait(timeout=30) == 0


def test_continue_releases_reused_root_without_signalling(runtime: Runtime) -> None:
    runtime.orphan()
    sleeper = subprocess.Popen(["sleep", "120"], start_new_session=True)
    runtime.processes.append(sleeper)
    runtime.groups.add(sleeper.pid)
    projection = runtime.root / "runtime/spawns/p1/process_scopes.json"
    data = json.loads(projection.read_text())
    scope = data["scopes"][0]
    scope["root_pid"] = scope["pgid"] = sleeper.pid
    scope["root_created_at_epoch"] = psutil.Process(sleeper.pid).create_time() - 60
    projection.write_text(json.dumps(data))
    process = runtime.cli(*continue_args(False))
    runtime.started("p2")
    (runtime.root / "p2.exit").touch()
    assert process.wait(timeout=30) == 0
    assert alive(sleeper.pid), "reused PID was signalled"
    assert json.loads(projection.read_text())["released"]


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
    # Background CLI exit precedes worker adoption. Observe every successful
    # launch before the expected failure so teardown owns all resulting groups.
    for number in range(2, 2 + codes.count(0)):
        runtime.started(f"p{number}")
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


@pytest.mark.parametrize("legacy", [False, True], ids=["locked", "lockless"])
def test_continue_io_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool,
) -> None:
    from meridian.lib.launch.continue_sweep import sweep_before_continue
    from meridian.lib.state import spawn_store
    from meridian.lib.state.launch_lock import LaunchSlot
    from meridian.lib.state.spawn import repository
    from tests.support.call_probe import IOProbe

    root = tmp_path / "runtime"
    for number in range(1, 4):
        with LaunchSlot() as slot:
            spawn_store.start_spawn(
                root, spawn_id=f"p{number}", chat_id=f"c{number}", model="test", agent="",
                harness="claude", prompt="test", launch_lock=None if legacy else slot,
                started_at="2020-01-01T00:00:00+00:00",
            )
    # Isolate added sweep I/O: full continue also reads source/session metadata
    # for replay. CLI behavior is covered above, without faking that pipeline.
    probe = IOProbe(monkeypatch)
    probe.watch("scan", repository.scan_spawn_ids)
    probe.watch("read", repository.read_state)
    for source, count in enumerate((240, 2400, 12000), start=1):
        for number in range(4, count + 1):
            (root / "spawns" / f"p{number}").mkdir(exist_ok=True)
        before = probe.count("read")
        sweep_before_continue(root, f"p{source}", chat_id=f"c{source}")
        assert probe.count("scan") == 0
        assert probe.count("read") - before == 1 + int(legacy)
        assert all(call.args[1] == f"p{source}" for call in probe.calls("read")[before:])
