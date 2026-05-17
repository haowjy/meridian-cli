from __future__ import annotations

from meridian.lib.hooks.builtin_registry import get_default_events
from meridian.lib.hooks.config import _apply_name_overrides, _hooks_from_payload


def test_unnamed_builtin_hooks_with_different_remotes_do_not_override_each_other() -> None:
    docs_remote = "git@github.com:team/docs.git"
    kb_remote = "git@github.com:team/kb.git"
    hooks = _apply_name_overrides(
        _hooks_from_payload(
            {
                "hooks": [
                    {"builtin": "git-autosync", "remote": docs_remote},
                    {"builtin": "git-autosync", "remote": kb_remote},
                ]
            },
            source="project",
        )
    )

    default_events = set(get_default_events("git-autosync"))
    docs_hooks = tuple(hook for hook in hooks if hook.remote == docs_remote)
    kb_hooks = tuple(hook for hook in hooks if hook.remote == kb_remote)
    assert len(docs_hooks) == len(default_events)
    assert len(kb_hooks) == len(default_events)
    assert {hook.event for hook in docs_hooks} == default_events
    assert {hook.event for hook in kb_hooks} == default_events

    docs_names = {hook.name for hook in docs_hooks}
    kb_names = {hook.name for hook in kb_hooks}
    assert len(docs_names) == 1
    assert len(kb_names) == 1
    assert docs_names != kb_names
    assert next(iter(docs_names)).startswith("git-autosync:")
    assert next(iter(kb_names)).startswith("git-autosync:")


def test_explicit_same_name_builtin_hooks_still_override_by_name_and_event() -> None:
    hooks = _apply_name_overrides(
        _hooks_from_payload(
            {
                "hooks": [
                    {
                        "name": "autosync-primary",
                        "builtin": "git-autosync",
                        "event": "work.done",
                        "remote": "git@github.com:team/docs.git",
                        "priority": 1,
                    },
                    {
                        "name": "autosync-primary",
                        "builtin": "git-autosync",
                        "event": "work.done",
                        "remote": "git@github.com:team/kb.git",
                        "priority": 9,
                    },
                ]
            },
            source="project",
        )
    )

    assert len(hooks) == 1
    assert hooks[0].name == "autosync-primary"
    assert hooks[0].event == "work.done"
    assert hooks[0].remote == "git@github.com:team/kb.git"
    assert hooks[0].priority == 9


def test_duplicate_unnamed_builtin_hooks_with_same_remote_deterministically_override() -> None:
    remote = "git@github.com:team/docs.git"
    hooks = _apply_name_overrides(
        _hooks_from_payload(
            {
                "hooks": [
                    {"builtin": "git-autosync", "remote": remote, "priority": 1},
                    {"builtin": "git-autosync", "remote": remote, "priority": 7},
                ]
            },
            source="project",
        )
    )

    default_events = set(get_default_events("git-autosync"))
    assert len(hooks) == len(default_events)
    assert {hook.event for hook in hooks} == default_events
    assert {hook.priority for hook in hooks} == {7}
    names = {hook.name for hook in hooks}
    assert len(names) == 1
    assert next(iter(names)).startswith("git-autosync:")
