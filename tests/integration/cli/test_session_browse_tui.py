from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from meridian.cli.browse import tui as browse_tui
from meridian.cli.browse.tui import Lane, SearchProgress, SearchRequest, run_browse_picker
from meridian.lib.ops.session_list import SessionListOutput, SessionListRow
from meridian.lib.ops.session_reentry import Blocked, Resume
from meridian.lib.ops.session_search import SubsetSearchStep
from meridian.lib.state import session_store


def _listing() -> SessionListOutput:
    now = datetime.now(UTC).isoformat()
    rows = tuple(
        SessionListRow(
            chat_id=chat_id,
            activity_at=now,
            live=False,
            reentry=Resume(chat_id),
            agent="coder",
            model="gpt",
            work_label=work,
            task_cwd=f"/tmp/{work}",
        )
        for chat_id, work in (("c1", "alpha"), ("c2", "beta"))
    )
    return SessionListOutput(rows=rows, total_count=2)


def test_headless_picker_filters_then_activates_visible_row(tmp_path) -> None:
    selected: list[str] = []

    def resolve(chat_id: str):
        selected.append(chat_id)
        return Resume(chat_id)

    with create_pipe_input() as pipe:
        pipe.send_text("beta\r")
        decision = run_browse_picker(
            _listing(),
            tmp_path.as_posix(),
            resolve,
            input=pipe,
            output=DummyOutput(),
        )

    assert selected == ["c2"]
    assert decision == Resume("c2")


def test_headless_picker_blocked_enter_stays_open_until_quit(tmp_path) -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\rq")
        decision = run_browse_picker(
            _listing(),
            tmp_path.as_posix(),
            lambda _chat_id: Blocked("no harness session recorded; cannot resume or fork"),
            input=pipe,
            output=DummyOutput(),
        )

    assert decision is None


def test_headless_picker_ctrl_c_exits_130(tmp_path) -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        with pytest.raises(SystemExit) as exc_info:
            run_browse_picker(
                _listing(),
                tmp_path.as_posix(),
                lambda chat_id: Resume(chat_id),
                input=pipe,
                output=DummyOutput(),
            )
    assert exc_info.value.code == 130


def test_lane_discards_completion_from_replaced_request() -> None:
    started = threading.Event()
    release = threading.Event()
    invalidated = threading.Event()

    def worker(request, _current, post) -> None:
        started.set()
        assert release.wait(timeout=1)
        post(request)

    first = object()
    second = object()
    lane: Lane[object, object] = Lane(worker, invalidated.set)
    try:
        lane.submit(first)
        assert started.wait(timeout=1)
        lane.submit(second)
        release.set()
        deadline = time.monotonic() + 1
        results: list[object] = []
        while time.monotonic() < deadline and second not in results:
            invalidated.wait(timeout=0.05)
            invalidated.clear()
            results.extend(lane.drain())
    finally:
        lane.close()

    assert results == [second]


def test_search_worker_does_not_open_next_transcript_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def iter_steps(*, project_root: str, chat_ids: tuple[str, ...], query: str):
        _ = project_root, query
        for chat_id in chat_ids:
            opened.append(chat_id)
            yield SubsetSearchStep(chat_id, matched=False)

    monkeypatch.setattr(
        "meridian.lib.ops.session_search.iter_session_subset_search", iter_steps
    )
    active = True
    posted: list[object] = []

    def post(result: object) -> None:
        nonlocal active
        posted.append(result)
        active = False

    browse_tui._search_worker("/tmp/project")(
        SearchRequest("needle", ("c1", "c2")),
        lambda: active,
        post,
    )

    assert opened == ["c1"]
    assert posted == [SearchProgress(1, 2)]


def _run_meridian(args: list[str], *, cwd: Path, meridian_home: Path):
    env = os.environ.copy()
    for key in tuple(env):
        if key.upper().startswith("MERIDIAN_"):
            env.pop(key, None)
    env.update({"MERIDIAN_HOME": meridian_home.as_posix(), "TERM": "dumb"})
    return subprocess.run(
        [sys.executable, "-m", "meridian", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_plain_browse_and_bare_continue_are_identical(tmp_path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_id = "session-browse-cli"
    (project_root / "meridian.toml").write_text(
        f'[project]\nid = "{project_id}"\n', encoding="utf-8"
    )
    meridian_home = tmp_path / "meridian-home"
    runtime_root = meridian_home / "projects" / project_id
    runtime_root.mkdir(parents=True)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="77777777-7777-4777-8777-777777777777",
        model="gpt-5.4",
        agent="coder",
        kind="primary",
    )
    session_store.stop_session(runtime_root, chat_id)

    browse = _run_meridian(
        ["session", "browse", "--plain"], cwd=project_root, meridian_home=meridian_home
    )
    bare = _run_meridian(["--continue"], cwd=project_root, meridian_home=meridian_home)

    assert browse.returncode == bare.returncode == 0
    assert browse.stderr == bare.stderr == ""
    assert re.sub(r"\b\d+[smhd]\b", "AGE", browse.stdout) == re.sub(
        r"\b\d+[smhd]\b", "AGE", bare.stdout
    )
    assert "C-ID" in browse.stdout
    assert chat_id in browse.stdout
