from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import psutil

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
