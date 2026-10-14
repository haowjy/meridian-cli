from pathlib import Path

import pytest

from meridian.lib.hooks.config import load_hooks_config


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "event",
            'event = "bogus"',
            "expected one of [spawn.created, spawn.finalized, spawn.running, spawn.start, "
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

    assert str(exc_info.value) == f"Invalid value for 'project.hooks[1].{field}': {expected}."


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
        "Invalid value for 'project.hooks[1].when.status': expected values in "
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

    hook = load_hooks_config(tmp_path).hooks[0]

    assert hook.event == "spawn.finalized"
    assert hook.interval == "5m"
    assert hook.failure_policy == "warn"
    assert hook.timeout_secs == 1
    assert hook.when is not None
    assert hook.when.status == ("success", "failure")
