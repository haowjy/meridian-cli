from pathlib import Path

import pytest

from meridian.lib.hooks.config import load_hooks_config


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "event",
            'event = "bogus"',
            "expected one of [notify.cache_expiring, notify.manual, notify.your_turn, "
            "spawn.created, spawn.finished, spawn.started, work.activated, work.created, "
            "work.done], "
            "got 'bogus'",
        ),
        (
            "event",
            'event = "spawn.start"',
            "'spawn.start' was removed; use 'spawn.started'",
        ),
        (
            "event",
            'event = "work.start"',
            "'work.start' was removed; use 'work.activated'",
        ),
        (
            "event",
            'event = "spawn.running"',
            "'spawn.running' was removed; use 'spawn.started'",
        ),
        (
            "event",
            'event = "spawn.finalized"',
            "'spawn.finalized' was removed; use 'spawn.finished'",
        ),
        (
            "event",
            'event = "work.started"',
            "'work.started' was removed; use 'work.activated'",
        ),
        (
            "event",
            'event = "session.responded"',
            "'session.responded' was removed; use 'notify.your_turn'",
        ),
        (
            "event",
            'event = "session.cache_expiring"',
            "'session.cache_expiring' was removed; use 'notify.cache_expiring'",
        ),
        (
            "interval",
            'event = "spawn.started"\ninterval = "soon"',
            "expected interval format '\\d+[smhd]', got 'soon'",
        ),
        (
            "failure_policy",
            'event = "spawn.started"\nfailure_policy = "warn"',
            "'failure_policy' was removed; hook failures always continue",
        ),
        (
            "timeout_secs",
            'event = "spawn.started"\ntimeout_secs = 0',
            "expected int > 0, got 0",
        ),
    ],
)
def test_hook_scalar_constraints_are_enforced_by_the_config_model(
    tmp_path: Path,
    field: str,
    value: str,
    expected: str,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "invalid"\n'
        'command = "echo invalid"\n'
        f"{value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    assert str(exc_info.value) == (
        f"hook 'invalid': Invalid value for 'project.hooks[1].{field}': {expected}."
    )


def test_hook_status_constraint_is_enforced_by_the_config_model(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "invalid"\n'
        'event = "spawn.finished"\n'
        'command = "echo invalid"\n'
        "[hooks.when]\n"
        'status = ["bogus"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    assert str(exc_info.value) == (
        "hook 'invalid': Invalid value for 'project.hooks[1].when.status': expected values in "
        "[cancelled, failure, success, timeout], got 'bogus'."
    )


def test_hook_config_model_normalizes_closed_values(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "normalized"\n'
        'event = " spawn.finished "\n'
        'command = "echo normalized"\n'
        'interval = " 5m "\n'
        "timeout_secs = 1\n"
        "[hooks.when]\n"
        'status = [" success ", "failure"]\n'
        'kind = [" child ", "streaming"]\n',
        encoding="utf-8",
    )

    hook = next(hook for hook in load_hooks_config(tmp_path).hooks if hook.name == "normalized")

    assert hook.event == "spawn.finished"
    assert hook.interval == "5m"
    assert hook.timeout_secs == 1
    assert hook.when is not None
    assert hook.when.status == ("success", "failure")
    assert hook.when.kind == ("child", "streaming")


@pytest.mark.parametrize(
    ("event", "condition", "expected"),
    [
        (
            "spawn.started",
            'status = ["success"]',
            "condition is only available for 'spawn.finished', got 'spawn.started'",
        ),
        (
            "work.activated",
            'kind = ["child"]',
            "condition is unavailable for 'work.activated'",
        ),
        (
            "work.created",
            'agent = "coder"',
            "condition is unavailable for 'work.created'",
        ),
    ],
)
def test_hook_conditions_reject_events_that_cannot_supply_their_context(
    tmp_path: Path,
    event: str,
    condition: str,
    expected: str,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "impossible"\n'
        f'event = "{event}"\n'
        'command = "echo impossible"\n'
        "[hooks.when]\n"
        f"{condition}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    assert expected in str(exc_info.value)


@pytest.mark.parametrize(
    "condition",
    [
        'status = ["success"]',
        'agent = "coder"',
        'kind = ["child"]',
    ],
)
def test_builtin_default_conditions_disable_incompatible_override_events(
    tmp_path: Path,
    condition: str,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "filtered-defaults"\n'
        'builtin = "git-autosync"\n'
        'options = { path = "." }\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.local.toml").write_text(
        "[[hooks]]\n"
        'name = "filtered-defaults"\n'
        'builtin = "git-autosync"\n'
        'options = { path = "." }\n'
        "[hooks.when]\n"
        f"{condition}\n",
        encoding="utf-8",
    )

    events = {
        hook.event: (hook.source, hook.enabled)
        for hook in load_hooks_config(tmp_path).hooks
        if hook.name == "filtered-defaults"
    }

    assert events == {
        "spawn.finished": ("local", True),
        "work.activated": ("local", False),
        "work.done": ("local", False),
    }


def test_builtin_default_conditions_error_when_no_default_event_can_match(
    tmp_path: Path,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "notify"\n'
        "[hooks.when]\n"
        'status = ["success"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    message = str(exc_info.value)
    assert "row had no 'event'" in message
    assert "set 'event' explicitly" in message


def test_a_notify_row_replaces_only_its_events_default_route(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "notify"\n'
        'event = "notify.cache_expiring"\n'
        "enabled = false\n",
        encoding="utf-8",
    )

    routes = {
        (hook.event, hook.source, hook.enabled)
        for hook in load_hooks_config(tmp_path).hooks
        if hook.builtin == "notify"
    }

    assert routes == {
        ("notify.cache_expiring", "project", False),
        ("notify.your_turn", "default", True),
        ("notify.manual", "default", True),
    }


def test_notify_route_rejects_an_unknown_channel_when_config_loads(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "notify"\n'
        'event = "notify.your_turn"\n'
        'options = { channels = ["phone"] }\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    assert "project.hooks[1].options.channels" in str(exc_info.value)
    assert "phone" in str(exc_info.value)
