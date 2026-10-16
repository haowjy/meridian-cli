"""Live regressions for pre-exec ownership and runner-death containment."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil
import pytest

from tests.conftest import posix_only

pytestmark = posix_only

# Runs in a separate runner: fault injection pauses exactly at scope registration,
# not at an arbitrary sleep after exec. Main and the branch use the same seams.
RUNNER = r"""
import asyncio, os, sys, time
from pathlib import Path
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import managed_backend, managed_stdio
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.state.spawn_store import start_spawn
from meridian.lib.state.paths import resolve_project_runtime_root_for_write

root = Path(sys.argv[1])
mode = sys.argv[2]
pause = sys.argv[3] == "pause"
runtime = resolve_project_runtime_root_for_write(root)
spawn = SpawnId("p1")
start_spawn(
    runtime,
    spawn_id=spawn,
    chat_id="c1",
    model="test",
    agent="test",
    harness="pi",
    prompt="test",
    status="running",
)
command = (
    "/bin/sh",
    "-c",
    'echo $$ > harness; trap "" TERM HUP; sleep 60 & echo $! > descendant; wait',
)


def boundary(pid):
    (root / "boundary").write_text(str(pid))
    while pause:
        time.sleep(0.01)


async def register(**kwargs):
    boundary(kwargs["process"].pid)
    return await original(**kwargs)


async def run():
    global original
    if mode == "stdio":
        original = managed_stdio.register_spawn_owned_process
        managed_stdio.register_spawn_owned_process = register
        handle = await managed_stdio.launch_managed_stdio(
            config=ConnectionConfig(
                spawn_id=spawn,
                harness_id=HarnessId.PI,
                prompt="",
                control_root=root,
                child_env={},
                runtime_root=runtime,
            ),
            harness_id=HarnessId.PI,
            command=command,
            env=os.environ.copy(),
            cwd=str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout_limit=65536,
            kill_grace_seconds=0.1,
            terminate_reason="test",
        )
    else:
        # The native server is a child of a wrapper, not the PDEATHSIG target.
        backend_command = (sys.executable, '-c',
            'import subprocess; subprocess.Popen(' + repr(command) + ').wait()')
        original = managed_backend.register_spawn_owned_process
        managed_backend.register_spawn_owned_process = register
        handle = await managed_backend.launch_managed_backend(
            managed_backend.ManagedBackendConfig(
                spawn, HarnessId.CODEX, backend_command, root, os.environ.copy(), root
            ),
            stderr=asyncio.subprocess.DEVNULL,
        )
    await handle.process.wait()


if mode == "primary":
    from meridian.lib.launch.process.runner import run_primary_process_with_capture

    def started(identity):
        boundary(getattr(identity, "pid", identity))

    run_primary_process_with_capture(
        command, root, os.environ.copy(), None, started, root / "launcher.log"
    )
else:
    asyncio.run(run())
"""


def _until(check, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("bounded observation did not become true")


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _runner_case(tmp_path: Path, *, mode: str, pause: bool) -> None:
    env = {
        **os.environ,
        "MERIDIAN_HOME": str(tmp_path / "home"),
        "_MERIDIAN_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    runner = subprocess.Popen(
        [sys.executable, "-c", RUNNER, str(tmp_path), mode, "pause" if pause else "go"],
        env=env,
        start_new_session=True,
    )
    child = None
    try:
        boundary = tmp_path / "boundary"
        _until(boundary.exists)
        child = int(boundary.read_text())
        if not pause:
            _until((tmp_path / "descendant").exists)
        runner.kill()
        runner.wait(timeout=5)
        if pause:
            _until(lambda: not _alive(child))
            assert not (tmp_path / "harness").exists(), "harness executed before registration"
        else:
            descendant = int((tmp_path / "descendant").read_text())
            native = int((tmp_path / "harness").read_text())
            _until(lambda: not any(_alive(pid) for pid in (child, native, descendant)))
    finally:
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child, signal.SIGKILL)
        with suppress(ProcessLookupError):
            os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=5)


def test_runner_killed_before_registration_runs_no_harness(tmp_path: Path) -> None:
    _runner_case(tmp_path, mode="stdio", pause=True)


def test_runner_sigkill_kills_stdio_harness_group(tmp_path: Path) -> None:
    _runner_case(tmp_path, mode="stdio", pause=False)


def test_runner_sigkill_kills_reexeced_backend_group(tmp_path: Path) -> None:
    _runner_case(tmp_path, mode="backend", pause=False)


def test_non_tty_primary_is_gated(tmp_path: Path) -> None:
    _runner_case(tmp_path, mode="primary", pause=True)


def test_guardian_not_a_child_of_harness(tmp_path: Path) -> None:
    from meridian.lib.platform.launch_gate import LaunchGate

    gate = LaunchGate()
    child = subprocess.Popen(
        gate.command(
            (
                sys.executable,
                "-c",
                "import os\ntry:\n while True: os.waitpid(-1, 0)\n"
                "except ChildProcessError: print('done')",
            )
        ),
        env=gate.environment(os.environ),
        pass_fds=(gate.read_fd,),
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        gate.open()
        output, _ = child.communicate(timeout=5)
        assert output == b"done\n"
        members = _until(
            lambda: [
                p
                for p in psutil.process_iter(["pid", "ppid", "status"])
                if p.info["status"] != psutil.STATUS_ZOMBIE and _pgid(p.pid) == child.pid
            ]
        )
        assert all(p.ppid() != child.pid for p in members)
        gate.stop()
        _until(lambda: all(not _alive(p.pid) for p in members))
    finally:
        gate.close()
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def _pgid(pid: int) -> int | None:
    with suppress(ProcessLookupError):
        return os.getpgid(pid)
    return None


FD_PROBE = r"""
import os, subprocess, sys
from meridian.lib.platform.launch_gate import LaunchGate

# Deliberately allocate around 9 and above dash's redirection range.
opened = [os.open("/dev/null", os.O_RDONLY) for _ in range(15)]
gate = LaunchGate()
os.dup2(gate.read_fd, int(sys.argv[1]))
os.close(gate.read_fd)
gate.read_fd = int(sys.argv[1])
p = subprocess.Popen(
    gate.command(("/bin/sh", "-c", 'test -z "$_MERIDIAN_GATE_FD$_MERIDIAN_GATE_SCOPE" && echo ok')),
    env=gate.environment(os.environ),
    pass_fds=(gate.read_fd,),
    stdout=subprocess.PIPE,
    start_new_session=True,
)
gate.open()
out, _ = p.communicate(timeout=5)
gate.stop()
assert p.returncode == 0 and out == b"ok\n", (p.returncode, out)
"""


def test_gate_fd_already_nine() -> None:
    subprocess.run([sys.executable, "-c", FD_PROBE, "9"], check=True, timeout=10)


def test_gate_fd_above_nine() -> None:
    subprocess.run([sys.executable, "-c", FD_PROBE, "13"], check=True, timeout=10)


@pytest.mark.parametrize("pty_output", [False, True])
def test_harness_exit_gives_eof_before_stop(pty_output: bool) -> None:
    import select

    from meridian.lib.platform import pty
    from meridian.lib.platform.launch_gate import LaunchGate

    gate = LaunchGate()
    unrelated_fd = os.open(sys.executable, os.O_RDONLY)
    os.set_inheritable(unrelated_fd, True)
    master, slave = pty.openpty() if pty_output else os.pipe()
    command = gate.command(("/bin/sh", "-c", "echo ready; read x; echo done"))
    env = gate.environment(os.environ)
    process = None
    if pty_output:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(master)
            os.login_tty(slave)
            gate.prepare_pty_exec()
            os.execve(command[0], command, env)
        input_fd = master
    else:
        process = subprocess.Popen(
            command,
            env=env,
            pass_fds=(gate.read_fd,),
            stdin=subprocess.PIPE,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        child_pid = process.pid
        assert process.stdin is not None
        input_fd = process.stdin.fileno()
    os.close(slave)
    try:
        target = os.readlink(f"/proc/self/fd/{gate.read_fd}") if sys.platform == "linux" else None
        gate.open()
        assert select.select([master], [], [], 5)[0]
        assert b"ready" in os.read(master, 4096)
        guardians = _until(
            lambda: [
                p
                for p in psutil.process_iter(["pid", "ppid", "status"])
                if p.pid != child_pid
                and p.info["status"] != psutil.STATUS_ZOMBIE
                and _pgid(p.pid) == child_pid
                and p.ppid() != child_pid
            ]
        )
        if target is not None:
            for guardian in guardians:
                links = [os.readlink(fd) for fd in Path(f"/proc/{guardian.pid}/fd").iterdir()]
                assert links and all(link in (target, "/dev/null") for link in links), links
        os.write(input_fd, b"quit\n")
        if process is not None:
            process.wait(timeout=5)
        else:
            _until(lambda: not _alive(child_pid))
            os.waitpid(child_pid, 0)
        while True:
            assert select.select([master], [], [], 5)[0], "output held open before stop"
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                assert pty_output and exc.errno == 5
                break
            if not chunk:
                break
        gate.stop()
        _until(lambda: all(not _alive(p.pid) for p in guardians))
    finally:
        gate.close()
        os.close(unrelated_fd)
        os.close(master)
        with suppress(ProcessLookupError):
            os.killpg(child_pid, signal.SIGKILL)
        if process is not None:
            process.wait(timeout=5)
        else:
            with suppress(ChildProcessError):
                os.waitpid(child_pid, 0)


def test_tty_stdin_primary_root_only(tmp_path: Path) -> None:
    # A separate session is the shell pipeline: sibling must survive, harness
    # deliberately survives TERM so the single-signal/no-delayed-KILL rule is visible.
    script = r"""
import os, pty, signal, subprocess, sys, time
from pathlib import Path
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.process.ports import TerminalFds

root = Path(sys.argv[1])
master, slave = pty.openpty()
os.dup2(slave, 0)
sibling = subprocess.Popen(["/bin/sleep", "60"])
running = SubprocessProcessLauncher().start(
    command=(
        "/bin/sh",
        "-c",
        'trap "echo term >> terms" TERM; echo $$ > harness; while :; do sleep .05; done',
    ),
    cwd=root,
    env=os.environ.copy(),
    output_log_path=None, terminal_fds=TerminalFds(0, 1, 2),
)
(root / "ids").write_text(f"{running.pid} {sibling.pid}")
assert running.identity.pgid is None
running.wait()
"""
    runner = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], start_new_session=True)
    child = sibling = None
    try:
        _until((tmp_path / "harness").exists)
        child, sibling = map(int, (tmp_path / "ids").read_text().split())
        runner.kill()
        runner.wait(timeout=5)
        _until((tmp_path / "terms").exists)
        time.sleep(2.2)  # Beyond the group-mode grace: root mode must not escalate.
        assert (tmp_path / "terms").read_text() == "term\n"
        assert _alive(child) and _alive(sibling)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=5)
