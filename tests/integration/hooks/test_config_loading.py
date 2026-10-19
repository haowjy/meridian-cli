from pathlib import Path

import pytest

from meridian.lib.hooks.config import load_hooks_config, load_hooks_config_report
from meridian.lib.ops.hooks import HookListInput, hooks_list_sync


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
            "interval",
            'event = "spawn.started"\ninterval = "soon"',
            "expected interval format '\\d+[smhd]', got 'soon'",
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
        f'[[hooks]]\nname = "invalid"\ncommand = "echo invalid"\n{value}\n',
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
    ("configured", "replacement", "extra_warning"),
    [
        ("spawn.start", "spawn.started", "This row will now run"),
        ("spawn.running", "spawn.started", None),
        ("spawn.finalized", "spawn.finished", None),
        ("work.start", "work.activated", "This row will now run"),
        ("work.started", "work.activated", None),
        ("session.responded", "notify.your_turn", None),
        ("session.cache_expiring", "notify.cache_expiring", None),
    ],
)
def test_deprecated_event_names_warn_and_register_on_their_replacement(
    tmp_path: Path,
    configured: str,
    replacement: str,
    extra_warning: str | None,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "deprecated-event"\n'
        f'event = "{configured}"\n'
        'command = "echo deprecated"\n',
        encoding="utf-8",
    )

    report = load_hooks_config_report(tmp_path)

    hook = next(hook for hook in report.config.hooks if hook.name == "deprecated-event")
    assert report.errors == ()
    assert len(report.warnings) == 1
    assert f"`{configured}` is deprecated; use `{replacement}`." in report.warnings[0]
    assert "It will be rejected in the next minor release." in report.warnings[0]
    if extra_warning is not None:
        assert extra_warning in report.warnings[0]
    assert hook.event == replacement
    assert hook.configured_event == configured
    assert hook.deprecated is True

    listed = next(
        item
        for item in hooks_list_sync(HookListInput(project_root=str(tmp_path))).hooks
        if item.name == "deprecated-event"
    )
    assert listed.event == replacement
    assert listed.status == "deprecated"


def test_deprecated_failure_policy_warns_and_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "old-policy"\n'
        'event = "spawn.finished"\n'
        'command = "echo deprecated"\n'
        'failure_policy = "abort"\n',
        encoding="utf-8",
    )

    report = load_hooks_config_report(tmp_path)

    hook = next(hook for hook in report.config.hooks if hook.name == "old-policy")
    assert report.errors == ()
    assert len(report.warnings) == 1
    assert (
        "`failure_policy` is deprecated; it has no effect and will be rejected "
        "in the next minor release."
    ) in report.warnings[0]
    assert hook.deprecated is True


@pytest.mark.parametrize(
    ("event", "condition", "expected"),
    [
        (
            "spawn.started",
            'status = ["success"]',
            "condition is only available for `spawn.finished`, got `spawn.started`",
        ),
        (
            "work.activated",
            'kind = ["child"]',
            "condition is unavailable for `work.activated`",
        ),
        (
            "work.created",
            'agent = "coder"',
            "condition is unavailable for `work.created`",
        ),
    ],
)
def test_impossible_explicit_conditions_warn_and_skip_the_row(
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

    report = load_hooks_config_report(tmp_path)

    assert report.errors == ()
    assert len(report.warnings) == 1
    assert expected in report.warnings[0]
    assert "the row is skipped" in report.warnings[0]
    assert all(hook.name != "impossible" for hook in report.config.hooks)


def test_deprecated_skipped_status_warns_and_drops_only_that_value(
    tmp_path: Path,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "old-status"\n'
        'event = "spawn.finished"\n'
        'command = "echo deprecated"\n'
        "[hooks.when]\n"
        'status = ["success", "skipped"]\n'
        "\n"
        "[[hooks]]\n"
        'name = "only-old-spawn-status"\n'
        'event = "spawn.finished"\n'
        'command = "echo never"\n'
        "[hooks.when]\n"
        'status = ["skipped"]\n'
        "\n"
        "[[hooks]]\n"
        'name = "only-old-work-status"\n'
        'event = "work.done"\n'
        'command = "echo never"\n'
        "[hooks.when]\n"
        'status = ["skipped"]\n',
        encoding="utf-8",
    )

    report = load_hooks_config_report(tmp_path)

    hook = next(hook for hook in report.config.hooks if hook.name == "old-status")
    assert report.errors == ()
    assert len(report.warnings) == 3
    assert '`when.status = ["skipped"]` is deprecated' in report.warnings[0]
    assert "has been dropped because it never matched" in report.warnings[0]
    assert hook.when is not None
    assert hook.when.status == ("success",)
    assert hook.deprecated is True
    assert all(
        hook.name not in {"only-old-spawn-status", "only-old-work-status"}
        for hook in report.config.hooks
    )
    for warning in report.warnings[1:]:
        assert "the row is skipped because it could never match" in warning


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
        '[[hooks]]\nbuiltin = "notify"\n[hooks.when]\nstatus = ["success"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_hooks_config(tmp_path)

    message = str(exc_info.value)
    assert "row had no 'event'" in message
    assert "set 'event' explicitly" in message


def test_a_notify_row_replaces_only_its_events_default_route(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[[hooks]]\nbuiltin = "notify"\nevent = "notify.cache_expiring"\nenabled = false\n',
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
