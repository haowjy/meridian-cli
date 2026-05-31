import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.cli import primary_launch
from meridian.cli.argv_normalization import SELF_FORK_REF_SENTINEL
from meridian.lib.launch.types import LaunchRequest, LaunchResult
from meridian.lib.state import spawn_store
from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata


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


def test_run_primary_launch_bare_from_resolves_to_context_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(command=("meridian",), exit_code=0)

    def _no_context_work(_root: Path, _ref: str) -> SimpleNamespace:
        return SimpleNamespace(work_id=None)

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)
    monkeypatch.setattr(primary_launch, "resolve_context_ref", _no_context_work)

    primary_launch.run_primary_launch(
        project_root=Path.cwd(),
        continue_ref=None,
        fork_ref=None,
        fork_fresh_ref=None,
        from_ref=SELF_FORK_REF_SENTINEL,
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

    request = cast("LaunchRequest", captured["request"])
    assert request.context_from == ("p42",)
    assert request.session.requested_harness_session_id is None
    assert request.session.continue_fork is False


def test_run_primary_launch_from_inherits_source_work_when_no_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(command=("meridian",), exit_code=0)

    def _no_ambient_work(_project_root: Path) -> None:
        return None

    def _context_work(_root: Path, _ref: str) -> SimpleNamespace:
        return SimpleNamespace(work_id="feature-x")

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)
    monkeypatch.setattr(primary_launch, "_ambient_work_id", _no_ambient_work)
    monkeypatch.setattr(primary_launch, "resolve_context_ref", _context_work)

    primary_launch.run_primary_launch(
        project_root=Path.cwd(),
        continue_ref=None,
        fork_ref=None,
        fork_fresh_ref=None,
        from_ref="p123",
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

    request = cast("LaunchRequest", captured["request"])
    assert request.work_id == "feature-x"


def test_run_primary_launch_from_keeps_explicit_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(command=("meridian",), exit_code=0)

    def _no_ambient_work(_project_root: Path) -> None:
        return None

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)
    monkeypatch.setattr(primary_launch, "_ambient_work_id", _no_ambient_work)

    primary_launch.run_primary_launch(
        project_root=Path.cwd(),
        continue_ref=None,
        fork_ref=None,
        fork_fresh_ref=None,
        from_ref="p123",
        model="",
        harness=None,
        agent=None,
        work="explicit-work",
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=True,
        passthrough=(),
    )

    request = cast("LaunchRequest", captured["request"])
    assert request.work_id == "explicit-work"


def test_run_primary_launch_from_keeps_ambient_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(command=("meridian",), exit_code=0)

    def _ambient_work(_project_root: Path) -> str:
        return "ambient-work"

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)
    monkeypatch.setattr(primary_launch, "_ambient_work_id", _ambient_work)

    primary_launch.run_primary_launch(
        project_root=Path.cwd(),
        continue_ref=None,
        fork_ref=None,
        fork_fresh_ref=None,
        from_ref="p123",
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

    request = cast("LaunchRequest", captured["request"])
    assert request.work_id is None


def test_run_primary_launch_forwards_primary_spawn_parity_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    reference = project_root / "design.md"
    reference.write_text("design notes", encoding="utf-8")
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(command=("meridian",), exit_code=0)

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)

    primary_launch.run_primary_launch(
        project_root=project_root,
        continue_ref=None,
        fork_ref=None,
        fork_fresh_ref=None,
        model="",
        harness=None,
        agent="tech-lead",
        work="",
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=True,
        passthrough=(),
        reference_files=("design.md",),
        prompt="Review the design.",
        skills=("planning", "dev-principles"),
        goal="  Converge on requirements  ",
    )

    request = cast("LaunchRequest", captured["request"])
    assert request.reference_files == (reference.as_posix(),)
    assert request.prompt == "Review the design."
    assert request.skills == ("planning", "dev-principles")
    assert request.goal == "Converge on requirements"


def test_run_primary_launch_from_with_continue_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    with pytest.raises(
        ValueError,
        match=re.escape("Cannot combine --from with --continue."),
    ):
        primary_launch.run_primary_launch(
            project_root=Path.cwd(),
            continue_ref="c123",
            fork_ref=None,
            fork_fresh_ref=None,
            from_ref=SELF_FORK_REF_SENTINEL,
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


@pytest.mark.parametrize(
    ("fork_ref", "fork_fresh_ref", "expected"),
    [
        (SELF_FORK_REF_SENTINEL, None, "Cannot combine --fork with --from (MVP limitation)."),
        (
            None,
            SELF_FORK_REF_SENTINEL,
            "Cannot combine --fork-fresh with --from (MVP limitation).",
        ),
    ],
)
def test_run_primary_launch_from_with_fork_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
    fork_ref: str | None,
    fork_fresh_ref: str | None,
    expected: str,
) -> None:
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    with pytest.raises(ValueError, match=re.escape(expected)):
        primary_launch.run_primary_launch(
            project_root=Path.cwd(),
            continue_ref=None,
            fork_ref=fork_ref,
            fork_fresh_ref=fork_fresh_ref,
            from_ref=SELF_FORK_REF_SENTINEL,
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


def test_run_primary_launch_continue_reports_pi_never_created_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", str(runtime_root))

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p201",
        chat_id="c201",
        model="openai-codex/gpt-5.4-mini",
        agent="coder",
        harness="pi",
        kind="primary",
        prompt="seed",
        harness_session_id=None,
    )
    spawn_dir = runtime_root / "spawns" / "p201"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    write_primary_metadata(
        spawn_dir,
        PrimaryMetadata(
            managed_backend=False,
            harness_session_id=None,
            harness_session_discovery="never_created",
            harness_session_discovery_detail="no_session_files_in_dir: /tmp/sessions",
        ),
    )

    monkeypatch.setattr(
        primary_launch,
        "resolve_session_target",
        lambda **_kwargs: primary_launch.ResolvedSessionTarget(
            harness_session_id=None,
            chat_id="c201",
            harness="pi",
            tracked=True,
        ),
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Session 'c201' has no Pi session — the original Pi session "
            "was ephemeral or never persisted a session."
        ),
    ):
        primary_launch.run_primary_launch(
            project_root=project_root,
            continue_ref="c201",
            fork_ref=None,
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


def test_run_primary_launch_continue_reports_pi_discovery_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", str(runtime_root))

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p301",
        chat_id="c301",
        model="openai-codex/gpt-5.4-mini",
        agent="coder",
        harness="pi",
        kind="primary",
        prompt="seed",
        harness_session_id=None,
    )
    spawn_dir = runtime_root / "spawns" / "p301"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    write_primary_metadata(
        spawn_dir,
        PrimaryMetadata(
            managed_backend=False,
            harness_session_id=None,
            harness_session_discovery="discovery_failed",
            harness_session_discovery_detail=(
                "no_matching_session: cwd=/tmp/repo after=100.0"
            ),
        ),
    )

    monkeypatch.setattr(
        primary_launch,
        "resolve_session_target",
        lambda **_kwargs: primary_launch.ResolvedSessionTarget(
            harness_session_id=None,
            chat_id="c301",
            harness="pi",
            tracked=True,
        ),
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Session 'c301' could not discover a Pi session — "
            "no_matching_session: cwd=/tmp/repo after=100.0."
        ),
    ):
        primary_launch.run_primary_launch(
            project_root=project_root,
            continue_ref="c301",
            fork_ref=None,
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


@pytest.mark.parametrize(
    ("continue_ref", "fork_ref", "fork_fresh_ref"),
    [
        ("c410", None, None),
        (None, "c410", None),
        (None, None, "c410"),
    ],
)
def test_run_primary_launch_carries_source_pi_session_dir_to_session_request(
    monkeypatch: pytest.MonkeyPatch,
    continue_ref: str | None,
    fork_ref: str | None,
    fork_fresh_ref: str | None,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        primary_launch,
        "resolve_session_target",
        lambda **_kwargs: primary_launch.ResolvedSessionTarget(
            harness_session_id="ses-pi-source",
            chat_id="c410",
            harness="pi",
            source_model="openai-codex/gpt-5.4-mini",
            source_agent="coder",
            source_work_id="w410",
            source_control_root="/tmp/control",
            source_execution_cwd="/tmp/task",
            source_pi_session_dir="/tmp/pi-sessions/custom",
            tracked=True,
        ),
    )

    def _fake_launch_primary(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(command=("meridian",), exit_code=0)

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)

    primary_launch.run_primary_launch(
        project_root=Path.cwd(),
        continue_ref=continue_ref,
        fork_ref=fork_ref,
        fork_fresh_ref=fork_fresh_ref,
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

    request = cast("LaunchRequest", captured["request"])
    assert request.session.source_pi_session_dir == "/tmp/pi-sessions/custom"
