from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil

from meridian.lib.platform import parent_watchdog
from meridian.lib.platform.detached_process import (
    link_child_lifetime_to_parent,
    release_parent_death_link,
)
from meridian.lib.platform.parent_watchdog import (
    WatchedProcess,
    process_scope_is_alive,
    watch_parent_until_exit,
)
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from tests.conftest import posix_only


def _process_is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def test_watchdog_entrypoint_imports_only_stdlib_and_psutil() -> None:
    script_path = Path(parent_watchdog.__file__)
    probe = """
import json
import runpy
import sys
from pathlib import Path

script_path = Path(sys.argv[1])
sys.path.insert(0, str(script_path.parent))
before = set(sys.modules)
runpy.run_path(str(script_path), run_name="_parent_watchdog_import_probe")
added = set(sys.modules) - before
print(json.dumps({
    "meridian": sorted(
        name for name in sys.modules
        if name == "meridian" or name.startswith("meridian.")
    ),
    "unexpected": sorted(
        name for name in added
        if name.split(".", 1)[0] not in sys.stdlib_module_names
        and name.split(".", 1)[0] not in {"psutil", "scope_termination"}
    ),
}))
"""

    completed = subprocess.run(
        (sys.executable, "-c", probe, str(script_path)),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {"meridian": [], "unexpected": []}


@posix_only
def test_parent_death_link_waits_for_watchdog_readiness() -> None:
    target = subprocess.Popen(
        (sys.executable, "-c", "import time;time.sleep(60)"),
        start_new_session=True,
    )
    link = None
    try:
        link = link_child_lifetime_to_parent(target.pid)
        assert link.parent_death_linked is True
        assert link.watchdog_process is not None
        assert link.watchdog_process.poll() is None
    finally:
        target.terminate()
        target.wait(timeout=5.0)
        release_parent_death_link(link)


@posix_only
def test_watchdog_terminates_group_after_scope_root_exits() -> None:
    root = subprocess.Popen(
        (
            sys.executable,
            "-c",
            (
                "import subprocess,sys;"
                "child=subprocess.Popen((sys.executable,'-c','import time;time.sleep(60)'));"
                "print(child.pid,flush=True)"
            ),
        ),
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid = 0
    try:
        root_created_at = psutil.Process(root.pid).create_time()
        assert root.stdout is not None
        child_pid = int(root.stdout.readline().strip())
        pgid = os.getpgid(child_pid)
        assert pgid == root.pid
        assert root.wait(timeout=5.0) == 0

        scope = ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id="watchdog-test",
            role="harness_backend",
            containment="posix_pgid",
            root_pid=root.pid,
            root_created_at_epoch=root_created_at,
            pgid=pgid,
            job_name=None,
            degraded_reason=None,
        )
        assert process_scope_is_alive(scope) is True

        watch_parent_until_exit(
            parent=WatchedProcess(pid=-1, created_at_epoch=0.0),
            target_scope=scope,
            parent_alive=lambda _parent: False,
        )

        deadline = time.monotonic() + 5.0
        while _process_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_is_running(child_pid) is False
    finally:
        if root.poll() is None:
            root.terminate()
            root.wait(timeout=5.0)
        if child_pid > 0:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


@posix_only
def test_watchdog_kills_backend_wrapper_group_when_launcher_is_killed(
    tmp_path: Path,
) -> None:
    root_pid_path = tmp_path / "backend-wrapper.pid"
    grandchild_pid_path = tmp_path / "backend-grandchild.pid"
    parent_code = """
import os
import subprocess
import sys
from pathlib import Path

from meridian.lib.platform.detached_process import link_child_lifetime_to_parent

root_pid_path, grandchild_pid_path = sys.argv[1:]
root = subprocess.Popen(
    ("sh", "-c", f"sleep 300 & echo $! > {grandchild_pid_path}; wait"),
    start_new_session=True,
)
link = link_child_lifetime_to_parent(root.pid)
if not link.parent_death_linked:
    raise RuntimeError("watchdog unavailable")
Path(root_pid_path).write_text(str(root.pid))
root.wait()
""".strip()
    parent = subprocess.Popen(
        (
            sys.executable,
            "-c",
            parent_code,
            str(root_pid_path),
            str(grandchild_pid_path),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    root_pid = 0
    grandchild_pid = 0
    try:
        _wait_for_file(root_pid_path)
        _wait_for_file(grandchild_pid_path)
        root_pid = int(root_pid_path.read_text(encoding="utf-8"))
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5.0)

        deadline = time.monotonic() + 8.0
        while (
            _process_is_running(root_pid) or _process_is_running(grandchild_pid)
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_is_running(root_pid) is False
        assert _process_is_running(grandchild_pid) is False
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5.0)
        for pid in (root_pid, grandchild_pid):
            if pid > 0 and _process_is_running(pid):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
