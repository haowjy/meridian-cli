"""The machine inbox's write sequence (`lib/inbox/.context/CONTEXT.md`, "Write") against a
writer killed at each step, and against writers that race."""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.inbox import store
from meridian.lib.inbox.models import Message, Party, Via
from tests.support.process_race import run_spawn_race_or_skip

if TYPE_CHECKING:
    import pytest

_KILLED = 86
# Every call that can change what is on disk. A kill lands before one.
_STEPS = ("open", "symlink", "link", "rename", "replace", "unlink")


def _message(ref: str) -> Message:
    return Message(
        received=datetime(2026, 11, 27, tzinfo=UTC),
        sender=Party(kind="terminal", machine="homebox"),
        via=Via(program="slack", ref=ref, sender="alice"),
        text=f"message {ref}",
    )


def _file_killed_before_step(ref: str, step: int) -> None:
    """File the message in this process and `_exit` as the `step`th call is reached."""

    reached = 0

    def killing(call: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            nonlocal reached
            reached += 1
            if reached == step:
                os._exit(_KILLED)
            return call(*args, **kwargs)

        return wrapped

    for name in _STEPS:
        setattr(os, name, killing(getattr(os, name)))
    store.file_message(_message(ref))
    # Past the write, so the process's own teardown can't reach a step and pass for a kill.
    os._exit(0)


def _killed_writer(ref: str, step: int) -> bool:
    """Whether the writer was killed: `False` when it finished before reaching `step`."""

    process = multiprocessing.get_context("spawn").Process(
        target=_file_killed_before_step, args=(ref, step)
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode in (0, _KILLED)
    return process.exitcode == _KILLED


def _names(directory: Path) -> set[str]:
    return set(os.listdir(directory)) if directory.is_dir() else set()


def _left_behind(root: Path, ref: str, issued_before: int, names_before: set[str]) -> str:
    """Which row of the context's kill table the killed writer left."""

    names = _names(root / "messages") - names_before
    if f"m{issued_before + 1}.json" in names:
        return "a whole message"
    if any(name.endswith(".create") for name in names):
        return "a staged message"
    if os.path.lexists(store.origin_link(root, "slack", ref)):
        return "a link to no file"
    if store.read_issued(root) == issued_before + 1:
        return "a number with no message"
    return "nothing"


def _waiting_by_ref() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for entry in store.entries(taken=False):
        assert entry.message.via is not None and entry.message.via.ref is not None
        found.setdefault(entry.message.via.ref, []).append(entry.id)
    return found


def test_a_writer_killed_at_any_step_never_loses_duplicates_or_reuses_a_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = store.inbox_dir()
    seen_states: set[str] = set()
    filed: dict[str, str] = {}
    step = 0
    while True:
        step += 1
        ref = f"T1-{step}"
        issued_before = store.read_issued(root) or 0
        names_before = _names(root / "messages")
        before = {
            ref_: (root / "messages" / f"{id_}.json").read_bytes() for ref_, id_ in filed.items()
        }

        if not _killed_writer(ref, step):
            break
        state = _left_behind(root, ref, issued_before, names_before)
        seen_states.add(state)

        # A reader sees whole messages only, and every earlier one unchanged.
        listed = _waiting_by_ref()
        assert capsys.readouterr().err == ""
        assert set(listed) <= {*filed, ref}
        assert listed.get(ref, []) == (
            [f"m{issued_before + 1}"] if state == "a whole message" else []
        )
        assert {
            ref_: (root / "messages" / f"{id_}.json").read_bytes() for ref_, id_ in filed.items()
        } == before

        # The next writer of the same message files it once, under a number never issued.
        message_id = store.file_message(_message(ref))
        assert store.file_message(_message(ref)) == message_id
        if state == "a whole message":
            assert message_id == f"m{issued_before + 1}"
        else:
            assert store.message_number(message_id) > issued_before
        filed[ref] = message_id
        assert _waiting_by_ref() == {ref_: [id_] for ref_, id_ in filed.items()}

    assert seen_states >= {
        "a number with no message",
        "a link to no file",
        "a staged message",
        "a whole message",
    }, f"the sweep stopped after {step} steps"


def _file(ref: str) -> str:
    return store.file_message(_message(ref))


def test_writers_racing_on_one_outside_ref_file_one_message() -> None:
    ids = run_spawn_race_or_skip(_file, [("T1-race",) for _ in range(8)])

    assert set(ids) == {"m1"}
    assert _waiting_by_ref() == {"T1-race": ["m1"]}
