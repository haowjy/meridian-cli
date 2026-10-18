import pytest

from meridian.lib.harness.idle_types import BackgroundTask
from meridian.lib.idle.wake import ExpectedWake, SpawnCommand, expected_wake, spawn_command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('meridian spawn -a coder -p "fix it"', SpawnCommand(waits=False)),
        ("cd x && uv run meridian -C . spawn wait p3", SpawnCommand(waits=True)),
        ("meridian spawn wait --yield-after-secs 7200", SpawnCommand(True, 7200.0)),
        (
            "/usr/bin/meridian --format=json spawn wait --yield-after-secs=90",
            SpawnCommand(True, 90.0),
        ),
        ('meridian report create "spawn later"', None),
        ("npm run dev", None),
        ("echo 'unterminated", None),
    ],
    ids=[
        "launch",
        "wrapped-wait",
        "yield",
        "global-options",
        "spawn-in-text",
        "other",
        "unparseable",
    ],
)
def test_spawn_command_reads_meridian_grammar_through_wrappers(
    command: str,
    expected: SpawnCommand | None,
) -> None:
    assert spawn_command(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "while true; do meridian spawn list; sleep 60; done",
            ExpectedWake(maybe=True),
        ),
        ("watch -n 30 meridian spawn list", ExpectedWake(maybe=True)),
        ("meridian spawn wait p1 && npm run dev", ExpectedWake(maybe=True)),
        (
            "meridian spawn wait p1",
            ExpectedWake(certain="meridian spawns running", by_ms=3_001_000),
        ),
        (
            "cd x && meridian spawn wait p1",
            ExpectedWake(certain="meridian spawns running", by_ms=3_001_000),
        ),
        (
            "uv run meridian spawn -a coder -p 'fix it'",
            ExpectedWake(certain="meridian spawns running"),
        ),
        (
            "meridian -C dir spawn wait",
            ExpectedWake(certain="meridian spawns running", by_ms=3_001_000),
        ),
        ("meridian spawn --bg -a coder -p 'fix it'", ExpectedWake(maybe=True)),
        ("meridian spawn list", ExpectedWake(maybe=True)),
        ("meridian spawn some-new-subcommand p1", ExpectedWake(maybe=True)),
        (
            "uv run --project ~/x/meridian meridian spawn wait p1",
            ExpectedWake(certain="meridian spawns running", by_ms=3_001_000),
        ),
        (
            "meridian spawn wait --timeout 120",
            ExpectedWake(certain="meridian spawns running", by_ms=7_201_000),
        ),
        (
            "meridian spawn wait --timeout 120 --yield-after-secs 60",
            ExpectedWake(certain="meridian spawns running", by_ms=7_201_000),
        ),
    ],
)
def test_shell_wake_certainty_follows_the_command_that_ends_the_shell(
    command: str,
    expected: ExpectedWake,
) -> None:
    assert expected_wake(
        (BackgroundTask("shell", command),),
        now_ms=1_000,
        default_yield_seconds=3000,
    ) == expected


def test_expected_wake_reduces_running_work() -> None:
    tasks = (
        BackgroundTask("agent"),
        BackgroundTask("shell", "meridian spawn wait p3 --yield-after-secs 600"),
        BackgroundTask("shell", "meridian spawn wait"),
        BackgroundTask("watcher"),
        BackgroundTask("shell", "npm run dev"),
    )

    assert expected_wake(tasks, now_ms=1_000, default_yield_seconds=3000) == ExpectedWake(
        certain="1 agent(s) running",
        by_ms=601_000,
        maybe=True,
    )
    assert expected_wake(
        (BackgroundTask("watcher"),), now_ms=0, default_yield_seconds=3000
    ) == ExpectedWake()
