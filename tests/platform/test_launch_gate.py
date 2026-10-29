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

from meridian.lib.launch.process.ports import TUI_TERMINATE_GRACE_SECONDS
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
from meridian.lib.state.paths import LaunchPaths, resolve_project_runtime_root_for_write

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
                launch=LaunchPaths(runtime, spawn, None),
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
        # The native server is a child of a wrapper, inside the guarded process group.
        backend_command = (sys.executable, '-c',
            'import subprocess; subprocess.Popen(' + repr(command) + ').wait()')
        original = managed_backend.register_spawn_owned_process
        managed_backend.register_spawn_owned_process = register
        handle = await managed_backend.launch_managed_backend(
            managed_backend.ManagedBackendConfig(
                LaunchPaths(runtime, spawn, None), HarnessId.CODEX, backend_command, root,
                os.environ.copy(), 0.1,
            ),
            stderr=asyncio.subprocess.DEVNULL,
        )
    await handle.process.wait()


if mode in ("primary", "native"):
    from meridian.lib.launch.process.runner import run_primary_process_with_capture
    from meridian.lib.launch.process.native_launcher import NativeProcessLauncher
    from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher

    def started(identity):
        boundary(identity.pid)

    run_primary_process_with_capture(
        command, root, os.environ.copy(), None,
        (NativeProcessLauncher() if mode == "native" else SubprocessProcessLauncher()),
        started, root / "launcher.log"
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


def _written(path: Path) -> str:
    """Wait until a file another process creates has its content, not just its name."""
    return _until(lambda: path.exists() and path.read_text().strip())


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
        child = int(_written(tmp_path / "boundary"))
        if not pause:
            _until((tmp_path / "descendant").exists)
        runner.kill()
        runner.wait(timeout=5)
        if pause:
            _until(lambda: not _alive(child))
            assert not (tmp_path / "harness").exists(), "harness executed before registration"
        else:
            descendant = int(_written(tmp_path / "descendant"))
            native = int(_written(tmp_path / "harness"))
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


@pytest.mark.parametrize("mode", ["primary", "native"])
def test_non_tty_primary_is_gated(tmp_path: Path, mode: str) -> None:
    _runner_case(tmp_path, mode=mode, pause=True)


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
        gate.adopt(child.pid)
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
from tests.support.launch_gate import gate_at_fd
gate = gate_at_fd(int(sys.argv[1]))
p = subprocess.Popen(
    gate.command(("/bin/sh", "-c",
        'test -z "$_MERIDIAN_GATE_FD$_MERIDIAN_GATE_SCOPE$_MERIDIAN_GATE_GRACE" && echo ok')),
    env=gate.environment(os.environ),
    pass_fds=(gate.read_fd,),
    stdout=subprocess.PIPE,
    start_new_session=True,
)
gate.adopt(p.pid)
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
def test_harness_exit_gives_eof_before_stop(pty_output: bool, tmp_path: Path) -> None:
    import select
    from concurrent.futures import ThreadPoolExecutor

    from meridian.lib.launch.process.ports import TerminalFds
    from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
    from meridian.lib.platform import pty
    from meridian.lib.platform.launch_gate import LaunchGate

    unrelated_fd = os.open(sys.executable, os.O_RDONLY)
    os.set_inheritable(unrelated_fd, True)
    master, slave = pty.openpty() if pty_output else os.pipe()
    command = ("/bin/sh", "-c", "echo ready; read x; echo done")
    pool = ThreadPoolExecutor(max_workers=1)
    process = None
    if pty_output:
        running = PtyProcessLauncher().start(
            command=command,
            cwd=tmp_path,
            env=dict(os.environ),
            output_log_path=None,
            terminal_fds=TerminalFds(slave, slave, slave),
        )
        child_pid, gate = running.identity.pid, running.gate
        completed = pool.submit(running.wait)
        input_fd = master
    else:
        gate = LaunchGate()
        process = subprocess.Popen(
            **gate.spawn_kwargs(command, os.environ),
            stdin=subprocess.PIPE,
            stdout=slave,
            stderr=slave,
        )
        child_pid = process.pid
        gate.adopt(child_pid)
        assert process.stdin is not None
        input_fd = process.stdin.fileno()
        gate.open()
    os.close(slave)
    try:
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
        if sys.platform == "linux":
            for guardian in guardians:
                links = [os.readlink(fd) for fd in Path(f"/proc/{guardian.pid}/fd").iterdir()]
                pipes = {link for link in links if link.startswith("pipe:")}
                assert len(pipes) == 1
                assert all(link == "/dev/null" or link in pipes for link in links), links
        os.write(input_fd, b"quit\n")
        if pty_output:
            # The actual relay waits for PTY EOF before reaping and sending stop.
            assert completed.result(timeout=5).exit_code == 0
        else:
            assert process is not None
            process.wait(timeout=5)
            while True:
                assert select.select([master], [], [], 5)[0], "output held open before stop"
                if not os.read(master, 4096):
                    break
            gate.release_after_reap(process.returncode)
        _until(lambda: all(not _alive(p.pid) for p in guardians))
    finally:
        gate.close()
        os.close(unrelated_fd)
        os.close(master)
        with suppress(ProcessLookupError):
            os.killpg(child_pid, signal.SIGKILL)
        if process is not None:
            process.wait(timeout=5)
        pool.shutdown(wait=True)


@pytest.mark.parametrize("launcher", ["SubprocessProcessLauncher", "NativeProcessLauncher"])
def test_tty_stdin_primary_root_only(tmp_path: Path, launcher: str) -> None:
    # A separate session is the shell pipeline: sibling must survive, harness
    # deliberately survives TERM so the single-signal/no-delayed-KILL rule is visible.
    script = r"""
import os, pty, signal, subprocess, sys, time
from pathlib import Path
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.process.native_launcher import NativeProcessLauncher
from meridian.lib.launch.process.ports import TerminalFds

root = Path(sys.argv[1])
master, slave = pty.openpty()
os.dup2(slave, 0)
sibling = subprocess.Popen(["/bin/sleep", "60"])
running = globals()[sys.argv[2]]().start(
    command=(
        "/bin/sh",
        "-c",
        'trap "echo term >> terms" TERM; echo $$ > harness; while :; do sleep .05; done',
    ),
    cwd=root,
    env=os.environ.copy(),
    output_log_path=None, terminal_fds=TerminalFds(0, 1, 2),
)
(root / "ids").write_text(f"{running.identity.pid} {sibling.pid}")
assert running.identity.pgid is None
assert os.getpgid(running.identity.pid) == os.getpgrp()
running.wait()
"""
    runner = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), launcher], start_new_session=True
    )
    child = sibling = None
    try:
        _until((tmp_path / "harness").exists)
        child, sibling = map(int, _written(tmp_path / "ids").split())
        runner.kill()
        runner.wait(timeout=5)
        _until((tmp_path / "terms").exists)
        time.sleep(
            TUI_TERMINATE_GRACE_SECONDS + 0.2
        )  # Beyond the group-mode grace: root mode must not escalate.
        assert (tmp_path / "terms").read_text() == "term\n"
        assert _alive(child) and _alive(sibling)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=5)


def test_native_cancel_leaves_one_term_to_scope_owner(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from meridian.lib.launch.process.native_launcher import NativeProcessLauncher
    from meridian.lib.launch.process.ports import TerminalFds
    from meridian.lib.platform.process_scope import scope_from_identity, terminate_scope_sync

    signals = tmp_path / "signals"
    ready = tmp_path / "ready"
    command = (
        sys.executable,
        "-c",
        "import signal, time\nfrom pathlib import Path\n"
        f"log = Path({str(signals)!r})\n"
        "signal.signal(signal.SIGTERM, lambda *_: log.open('a').write('TERM\\n'))\n"
        f"Path({str(ready)!r}).touch()\n"
        "while True: time.sleep(.01)\n",
    )
    with open(os.devnull, "r+b") as terminal:
        running = NativeProcessLauncher().start(
            command=command,
            cwd=tmp_path,
            env=dict(os.environ),
            output_log_path=None,
            terminal_fds=TerminalFds(terminal.fileno(), terminal.fileno(), terminal.fileno()),
        )
    scope = scope_from_identity(
        running.identity,
        scope_id="tui",
        role="harness_tui",
        owner_policy="session_owned",
        owner_id="native-cancel",
    )
    with ThreadPoolExecutor(max_workers=1) as worker:
        waiting = worker.submit(running.wait)
        try:
            _until(ready.exists)
            running.cancel_wait()
            assert waiting.result(timeout=5).exit_code == 130
            terminate_scope_sync(scope, grace_seconds=0.3, reason="signal_cancelled")
            running.terminate()
            assert running.process.poll() is not None
            assert signals.read_text().splitlines() == ["TERM"]
        finally:
            with suppress(ProcessLookupError):
                running.process.kill()
            running.process.wait(timeout=5)
            running.terminate()
