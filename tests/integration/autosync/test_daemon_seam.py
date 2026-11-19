"""The Python/Rust seam: a root record Python writes, a pass the built daemon runs, and
the state Python reads back."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from meridian.lib.autosync import state as autosync
from meridian.lib.platform.python_module import installed_executable

_GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=cwd, check=True, capture_output=True)


def _wait(done: object, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if done():  # type: ignore[operator]
            return
        time.sleep(0.1)
    raise AssertionError("timed out waiting for the daemon")


def test_a_registered_project_is_synced_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = installed_executable("meridian-sync")
    if daemon is None:
        pytest.skip("meridian-sync is not built in this environment")
    home = tmp_path / "home"
    monkeypatch.setenv("MERIDIAN_HOME", str(home))

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "kb").mkdir()
    (seed / "kb" / "a.md").write_text("a\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", "origin", "HEAD:main")

    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text(
        f'[context.kb]\nsource = "git"\nremote = "{remote}"\npath = "kb"\n'
    )
    registration = autosync.register_project(project)
    assert registration is not None
    written_at = registration.written_at
    contexts = autosync.git_contexts(project)
    assert [c.name for c in contexts] == ["kb"]

    proc = subprocess.Popen([daemon, "run", "--home", str(home), "--no-wait"])
    try:

        def synced() -> bool:
            [status] = autosync.context_sync(
                contexts,
                running=autosync.daemon_running(),
                service_is_off=False,
                file=autosync.read_daemon_file(),
                written_at=written_at,
                record_file=autosync.record_path(project).name,
            )
            answered = (status.entry or {}).get("answered")
            return status.state == "synced" and isinstance(answered, str) and answered >= written_at

        _wait(synced)
        assert (contexts[0].clone_path / "kb" / "a.md").read_text() == "a\n"
        assert autosync.read_daemon_file().text("version")
    finally:
        proc.terminate()
        assert proc.wait(timeout=30) == 0
    assert not autosync.daemon_running()
