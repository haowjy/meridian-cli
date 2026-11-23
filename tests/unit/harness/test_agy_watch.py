"""Pins when the ``agy`` watcher exits, which is what wakes an idle session."""

import os
import threading
from pathlib import Path

import pytest

from meridian.lib.harness.agy import inbox, watch
from meridian.lib.harness.agy.state import busy_path, process_dir, wake_path, watch_lock_path

CONVERSATION = "c1"


@pytest.fixture(autouse=True)
def proc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(watch, "POLL_SECONDS", 0.02)
    proc = process_dir(os.getpid())
    assert proc is not None
    proc.mkdir(parents=True)
    watch.watcher_running(proc, CONVERSATION)  # the Stop hook's test creates the lock file
    return proc


class _Watcher:
    """Runs the watcher in a thread so a test can change the files it reads."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys
        self.returned: list[int] = []
        self.thread = threading.Thread(
            target=lambda: self.returned.append(watch.run(os.getpid(), CONVERSATION)), daemon=True
        )
        self.thread.start()

    def wait(self, seconds: float) -> bool:
        self.thread.join(seconds)
        return not self.thread.is_alive()


def test_idle_session_wakes_for_a_message_that_arrives_after_the_watcher_started(
    capsys: pytest.CaptureFixture[str],
) -> None:
    watcher = _Watcher(capsys)
    assert not watcher.wait(0.2)

    inbox.enqueue(CONVERSATION, "hello", None)

    assert watcher.wait(3)
    assert watcher.returned == [0]
    out = capsys.readouterr().out
    assert "message arrived" in out
    assert f"meridian harness-watch agy --agy-pid {os.getpid()} --conversation c1" in out


def test_busy_session_is_never_woken_and_a_stuck_old_entry_is_not_a_reason(
    proc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox.enqueue(CONVERSATION, "stuck before the watcher started", None)
    busy = busy_path(proc_dir, CONVERSATION)
    busy.parent.mkdir(parents=True)
    busy.touch()
    watcher = _Watcher(capsys)
    assert not watcher.wait(0.2)  # it has noted the inbox as it stands

    inbox.enqueue(CONVERSATION, "arrives mid-turn", None)
    assert not watcher.wait(0.3)

    busy.unlink()
    assert watcher.wait(3)


def test_wake_file_wakes_an_idle_session_with_its_text(
    proc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wake = wake_path(proc_dir, CONVERSATION)
    wake.parent.mkdir(parents=True)
    wake.write_text("Keep the cache warm.")

    assert _Watcher(capsys).wait(3)
    assert "Keep the cache warm." in capsys.readouterr().out


def test_second_watcher_says_one_runs_and_exits(
    proc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _Watcher(capsys)
    assert not first.wait(0.2)

    assert watch.run(os.getpid(), CONVERSATION) == 0

    assert "already runs" in capsys.readouterr().out
    assert not first.wait(0.1)
    wake = wake_path(proc_dir, CONVERSATION)  # ends the first watcher with the test
    wake.parent.mkdir(parents=True)
    wake.write_text("done")
    assert first.wait(3)


def test_watcher_without_its_lock_file_prints_the_notice(
    proc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_lock_path(proc_dir, CONVERSATION).unlink()

    assert watch.run(os.getpid(), CONVERSATION) == 0

    assert "BypassSandbox true" in capsys.readouterr().out
