from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from meridian.cli.browse.model import (
    Activate,
    Backspace,
    BrowseModel,
    Character,
    Enter,
    Escape,
    Interrupt,
    Move,
    Quit,
    Search,
    StartSearch,
)
from meridian.cli.browse.render import render_footer, render_list
from meridian.cli.session_cmd import (
    Interactive,
    Plain,
    Refused,
    _exec_decision,
    resolve_browse_presentation,
)
from meridian.lib.ops.session_list import SessionListRow
from meridian.lib.ops.session_reentry import Blocked, Fork, Resume


def _row(chat_id: str, *, live: bool = False, work: str = "") -> SessionListRow:
    return SessionListRow(
        chat_id=chat_id,
        activity_at=datetime.now(UTC).isoformat(),
        live=live,
        reentry=Fork(chat_id) if live else Resume(chat_id),
        agent="coder",
        model="gpt",
        work_label=work,
        task_cwd=f"/tmp/{work or chat_id}",
    )


def _text(fragments: list[tuple[str, str]]) -> str:
    return "".join(fragment for _style, fragment in fragments)


def test_filter_navigation_and_activation() -> None:
    model = BrowseModel((_row("c1", work="alpha"), _row("c2", work="beta")))

    for character in "beta":
        assert model.handle_key(Character(character)) is None

    assert [row.chat_id for row in model.visible_rows] == ["c2"]
    assert model.handle_key(Enter()) == Activate("c2")
    model.handle_key(Backspace())
    assert model.filter_text == "bet"


def test_quit_key_table() -> None:
    model = BrowseModel((_row("c1"),))
    assert model.handle_key(Character("q")) == Quit()

    model.filter_text = "a"
    assert model.handle_key(Character("q")) is None
    assert model.filter_text == "aq"
    assert model.handle_key(Escape()) == Quit()
    assert model.handle_key(Interrupt()) == Quit(130)

    empty = BrowseModel(())
    assert empty.handle_key(Character("x")) is None
    assert empty.handle_key(Character("q")) == Quit()


def test_search_transitions_and_result_narrowing() -> None:
    model = BrowseModel((_row("c1"), _row("c2")))

    model.handle_key(Search())
    for character in "needle":
        model.handle_key(Character(character))
    assert model.handle_key(Enter()) == StartSearch("needle", ("c1", "c2"))

    model.apply_search_progress(1, 2)
    model.apply_search_done(frozenset({"c2"}), 2)
    assert [row.chat_id for row in model.visible_rows] == ["c2"]
    assert model.handle_key(Move(1)) is None
    assert model.handle_key(Escape()) is None
    assert [row.chat_id for row in model.visible_rows] == ["c1", "c2"]


def test_blocked_and_rendered_action_hints() -> None:
    live = _row("c1", live=True)
    blocked = _row("c2").model_copy(
        update={"reentry": Blocked("no harness session recorded; cannot resume or fork")}
    )
    model = BrowseModel((live, blocked))

    assert "fork → new session" in _text(render_footer(model, 100))
    assert "●" in _text(render_list(model, 100))
    model.highlight = 1
    model.apply_blocked("c2", "no harness session recorded; cannot resume or fork")
    assert "c2: no harness session" in _text(render_footer(model, 100))


def test_list_places_scroll_cursor_on_highlighted_row() -> None:
    model = BrowseModel(tuple(_row(f"c{index}") for index in range(20)))
    model.highlight = 15

    fragments = render_list(model, 80)

    cursor_markers = [
        index
        for index, (style, _text_value) in enumerate(fragments)
        if style == "[SetCursorPosition]"
    ]
    assert len(cursor_markers) == 1
    assert "c15" in fragments[cursor_markers[0] + 1][1]


@pytest.mark.parametrize(
    ("kwargs", "expected_type"),
    [
        ({"plain": True}, Plain),
        ({"stdin_tty": False}, Plain),
        ({"stdout_tty": False}, Plain),
        ({"term": "dumb"}, Plain),
        ({"term": None}, Plain),
        ({"managed": True}, Refused),
        ({}, Interactive),
    ],
)
def test_resolve_browse_presentation(kwargs: dict[str, object], expected_type: type) -> None:
    inputs: dict[str, object] = {
        "plain": False,
        "stdin_tty": True,
        "stdout_tty": True,
        "term": "xterm-256color",
        "managed": False,
        "ambient_chat": "c98",
    }
    inputs.update(kwargs)

    result = resolve_browse_presentation(**inputs)  # type: ignore[arg-type]

    assert isinstance(result, expected_type)
    if isinstance(result, Refused):
        assert "session c98" in result.reason


@pytest.mark.parametrize(
    ("decision", "verb"),
    [(Resume("c123"), "--continue"), (Fork("c123"), "--fork")],
)
def test_exec_decision_builds_primary_invocation(decision, verb: str) -> None:
    called: list[tuple[str, list[str]]] = []

    _exec_decision(
        decision,
        project_root="/tmp/project",
        config_file="/tmp/config.toml",
        exec_fn=lambda executable, argv: called.append((executable, argv)),
    )

    assert called == [
        (
            os.sys.executable,
            [
                os.sys.executable,
                "-m",
                "meridian",
                "-C",
                "/tmp/project",
                "--config",
                "/tmp/config.toml",
                verb,
                "c123",
            ],
        )
    ]
