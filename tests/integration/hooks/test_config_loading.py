from pathlib import Path

import pytest

from meridian.lib.hooks.config import load_hooks_config


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "event",
            'event = "bogus"',
            "expected one of [notify.manual, session.cache_expiring, session.responded, "
            "spawn.created, spawn.finalized, spawn.running, spawn.start, "
            "work.done, work.start, work.started], got 'bogus'",
        ),
        (
            "interval",
            'event = "spawn.start"\ninterval = "soon"',
            "expected interval format '\\d+[smhd]', got 'soon'",
        ),
        (
            "failure_policy",
            'event = "spawn.start"\nfailure_policy = "bogus"',
            "expected one of [fail, ignore, warn], got 'bogus'",
        ),
        (
            "timeout_secs",
            'event = "spawn.start"\ntimeout_secs = 0',
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
        'event = "spawn.finalized"\n'
        'command = "echo invalid"\n'
        "[hooks.when]\n"
        'status = ["bogus"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    assert str(exc_info.value) == (
        "hook 'invalid': Invalid value for 'project.hooks[1].when.status': expected values in "
        "[cancelled, failure, skipped, success, timeout], got 'bogus'."
    )


def test_hook_config_model_normalizes_closed_values(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "normalized"\n'
        'event = " spawn.finalized "\n'
        'command = "echo normalized"\n'
        'interval = " 5m "\n'
        'failure_policy = " warn "\n'
        "timeout_secs = 1\n"
        "[hooks.when]\n"
        'status = [" success ", "failure"]\n',
        encoding="utf-8",
    )

    hook = next(hook for hook in load_hooks_config(tmp_path).hooks if hook.name == "normalized")

    assert hook.event == "spawn.finalized"
    assert hook.interval == "5m"
    assert hook.failure_policy == "warn"
    assert hook.timeout_secs == 1
    assert hook.when is not None
    assert hook.when.status == ("success", "failure")


def test_a_notify_row_replaces_only_its_events_default_route(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "notify"\n'
        'event = "session.cache_expiring"\n'
        "enabled = false\n",
        encoding="utf-8",
    )

    routes = {
        (hook.event, hook.source, hook.enabled)
        for hook in load_hooks_config(tmp_path).hooks
        if hook.builtin == "notify"
    }

    assert routes == {
        ("session.cache_expiring", "project", False),
        ("session.responded", "default", True),
        ("notify.manual", "default", True),
    }


def test_notify_route_rejects_an_unknown_channel_when_config_loads(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "notify"\n'
        'event = "session.responded"\n'
        'options = { channels = ["phone"] }\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    assert "project.hooks[1].options.channels" in str(exc_info.value)
    assert "phone" in str(exc_info.value)
