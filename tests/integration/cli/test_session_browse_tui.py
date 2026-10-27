from __future__ import annotations

import contextlib
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

from meridian.cli.browse.lanes import Lanes
from meridian.cli.browse.tui import run_browse_picker
from meridian.lib.ops.session_list import SessionListOutput, SessionListRow
from meridian.lib.ops.session_reentry import Blocked, Resume, StartFrom
from meridian.lib.state import session_store


def _listing() -> SessionListOutput:
    now = datetime.now(UTC).isoformat()
    rows = tuple(
        SessionListRow(
            chat_id=chat_id,
            activity_at=now,
            reentry=Resume(chat_id),
            start_from=StartFrom(chat_id),
            agent="coder",
            model="gpt",
            work_label=work,
            task_cwd=f"/tmp/{work}",
        )
        for chat_id, work in (("c1", "alpha"), ("c2", "beta"))
    )
    return SessionListOutput(rows=rows, total_count=2)


def test_headless_picker_filters_then_activates_visible_row(tmp_path, monkeypatch) -> None:
    """Typing re-runs the session_list query; Enter acts on the row that query returned."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_id = "session-browse-query"
    (project_root / "meridian.toml").write_text(
        f'[project]\nid = "{project_id}"\n', encoding="utf-8"
    )
    meridian_home = tmp_path / "meridian-home"
    runtime_root = meridian_home / "projects" / project_id
    runtime_root.mkdir(parents=True)
    monkeypatch.setenv("MERIDIAN_HOME", meridian_home.as_posix())
    for work, session_id in (
        ("alpha", "11111111-1111-4111-8111-111111111111"),
        ("beta", "22222222-2222-4222-8222-222222222222"),
    ):
        chat_id = session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id=session_id,
            model="gpt-5.4",
            kind="primary",
        ).chat_id
        session_store.set_work_attachments(runtime_root, {chat_id: work})
        session_store.stop_session(runtime_root, chat_id)
    selected: list[str] = []

    def resolve(chat_id: str):
        selected.append(chat_id)
        return Resume(chat_id)

    with create_pipe_input() as pipe:

        def keys() -> None:
            pipe.send_text("beta")
            time.sleep(1.5)  # past the typing debounce and the query
            pipe.send_text("\r")
            time.sleep(5)
            with contextlib.suppress(Exception):
                pipe.send_text("\x03")  # fail instead of hanging if Enter found no row

        threading.Thread(target=keys, daemon=True).start()
        decision = run_browse_picker(
            _listing(),
            project_root.as_posix(),
            resolve,
            input=pipe,
            output=DummyOutput(),
        )

    assert selected == ["c2"]
    assert decision == Resume("c2")


def test_headless_picker_blocked_enter_stays_open_until_quit(tmp_path) -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\r\x1b")
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


def test_superseded_lane_request_never_applies() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    applied: list[str] = []
    done = threading.Event()

    def apply(value: str) -> None:
        applied.append(value)
        if value == "second":
            done.set()

    lanes = Lanes(lambda callback: callback(), _ready())

    def slow(_current, _post) -> str:
        first_started.set()
        assert release_first.wait(timeout=2)
        return "first"

    lanes.run("detail", slow, apply, lambda reason: applied.append(reason))
    assert first_started.wait(timeout=2)
    lanes.run(
        "detail", lambda _current, _post: "second", apply, lambda reason: applied.append(reason)
    )
    assert done.wait(timeout=2)
    release_first.set()
    time.sleep(0.1)
    lanes.close()

    assert applied == ["second"]


def _ready() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


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
    ).chat_id
    session_store.stop_session(runtime_root, chat_id)

    browse = _run_meridian(
        ["session", "browse", "--plain", "--limit", "1"],
        cwd=project_root,
        meridian_home=meridian_home,
    )
    bare = _run_meridian(
        ["--continue", "--plain", "--limit", "1"],
        cwd=project_root,
        meridian_home=meridian_home,
    )

    assert browse.returncode == bare.returncode == 0
    assert "Imported native sessions for 0 of 1 existing chats" in browse.stderr
    assert bare.stderr == ""
    assert re.sub(r"\b\d+[smhd]\b", "IDLE", browse.stdout) == re.sub(
        r"\b\d+[smhd]\b", "IDLE", bare.stdout
    )
    assert browse.stdout.splitlines()[0].split() == [
        "STALE",
        "TMUX",
        "AGENT",
        "IDLE",
        "WORK",
    ]


def test_plain_browse_keeps_unbound_chat_visible(tmp_path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_id = "session-browse-unbound"
    (project_root / "meridian.toml").write_text(
        f'[project]\nid = "{project_id}"\n', encoding="utf-8"
    )
    meridian_home = tmp_path / "meridian-home"
    runtime_root = meridian_home / "projects" / project_id
    runtime_root.mkdir(parents=True)
    (runtime_root / "legacy-native-import-v1.json").write_text("{}\n", encoding="utf-8")
    bound = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="88888888-8888-4888-8888-888888888888",
        native_store=(meridian_home / ".codex" / "sessions").as_posix(),
        model="gpt-5.4",
        kind="primary",
    ).chat_id
    unbound = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="99999999-9999-4999-8999-999999999999",
        model="gpt-5.4",
        kind="primary",
    ).chat_id
    session_store.stop_session(runtime_root, bound)
    session_store.stop_session(runtime_root, unbound)

    result = _run_meridian(
        ["session", "browse", "--plain"],
        cwd=project_root,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["STALE", "TMUX", "AGENT", "IDLE", "WORK"]
    assert len(lines) == 3  # both chats, bound and unbound; ids no longer appear in rows
