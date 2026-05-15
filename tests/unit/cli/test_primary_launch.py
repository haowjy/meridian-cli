import re
from pathlib import Path

import pytest

from meridian.cli import primary_launch
from meridian.cli.argv_normalization import SELF_FORK_REF_SENTINEL


def test_run_primary_launch_bare_fork_with_continue_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    with pytest.raises(
        ValueError,
        match=re.escape("Cannot combine --fork with --continue."),
    ):
        primary_launch.run_primary_launch(
            project_root=Path.cwd(),
            continue_ref="c123",
            fork_ref=SELF_FORK_REF_SENTINEL,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=True,
            passthrough=(),
        )


def test_run_primary_launch_bare_fork_fresh_with_continue_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    with pytest.raises(
        ValueError,
        match=re.escape("Cannot combine --fork-fresh with --continue."),
    ):
        primary_launch.run_primary_launch(
            project_root=Path.cwd(),
            continue_ref="c123",
            fork_ref=None,
            fork_fresh_ref=SELF_FORK_REF_SENTINEL,
            model="",
            harness=None,
            agent=None,
            work="",
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=True,
            passthrough=(),
        )


def test_run_primary_launch_unresolved_harness_message_uses_fork_fresh_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_target",
        lambda **_kwargs: primary_launch.ResolvedSessionTarget(
            harness_session_id=None,
            chat_id=None,
            harness=None,
            tracked=False,
        ),
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Session 'c123' not recognized by any harness. "
            "Use --harness to specify which harness owns this session."
        ),
    ):
        primary_launch.run_primary_launch(
            project_root=Path.cwd(),
            continue_ref=None,
            fork_ref=None,
            fork_fresh_ref="c123",
            model="",
            harness=None,
            agent=None,
            work="",
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=True,
            passthrough=(),
        )
