"""Session browse handoff contract: the argv a resumed/forked session is exec'd with."""

from __future__ import annotations

import os

import pytest

from meridian.cli.session_cmd import _exec_decision
from meridian.lib.ops.session_reentry import Fork, Resume


@pytest.mark.parametrize(
    ("decision", "verb"),
    [(Resume("c123"), "--continue"), (Fork("c123"), "--fork")],
)
def test_exec_decision_builds_primary_invocation(decision: Resume | Fork, verb: str) -> None:
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
