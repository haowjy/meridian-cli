# qa-validated: test-suite-redesign
"""Primary --continue launch-policy snapshot replay and legacy fallback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest  # noqa: TC002

from meridian.cli.primary_launch import run_primary_launch
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, LaunchResult, compile_prepared_policy_surface
from meridian.lib.launch.plan import build_primary_launch_runtime, build_primary_spawn_request
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.types import SessionMode
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root
from tests.support.fixtures import write_skill
from tests.support.launch import stub_bundle_request_and_resolve


def _state_root(project_root: Path) -> Path:
    mars_toml = project_root / "mars.toml"
    if not mars_toml.exists():
        mars_toml.write_text(
            '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
            encoding="utf-8",
        )
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_primary_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> None:
    model = launch_policy_snapshot.model if launch_policy_snapshot is not None else "gpt-5.3-codex"
    agent = (
        (launch_policy_snapshot.agent or "agent-a")
        if launch_policy_snapshot is not None
        else "agent-a"
    )
    skills = launch_policy_snapshot.skills if launch_policy_snapshot is not None else ("skill-a",)
    harness = launch_policy_snapshot.harness if launch_policy_snapshot is not None else "codex"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-primary",
        model=model,
        agent=agent,
        skills=skills,
        harness=harness,
        kind="primary",
        prompt="primary prompt",
        harness_session_id=harness_session_id,
        launch_policy_snapshot=launch_policy_snapshot,
    )


def test_primary_continue_replays_source_launch_policy_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    write_skill(project_root, "testing-principles")
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="agent-a",
        skills=("testing-principles",),
        loaded_skills=(
            SkillContent(
                name="testing-principles",
                description="testing-principles skill",
                path="/skills/testing-principles/SKILL.md",
                content="# testing-principles\n\nBe consistent.\n",
                skill_type="reference",
            ),
        ),
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
        tools={"write": "allow"},
        mcp_tools=("github",),
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p41",
        harness_session_id="session-41",
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "gpt-5.4")
    monkeypatch.setenv("MERIDIAN_AGENT", "agent-b")

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not re-resolve live policy")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref="session-41",
            continue_chat_id="c41",
        )

    with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
        run_primary_launch(
            project_root=project_root,
            continue_ref="p41",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    request = captured["request"]
    assert request.launch_policy_snapshot == snapshot
    assert request.agent is None
    assert request.model == ""
    assert request.skills == ()


def test_primary_continue_uses_legacy_bundle_resolution_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p42",
        harness_session_id="session-42",
        launch_policy_snapshot=None,
    )

    monkeypatch.setenv("MERIDIAN_MODEL", "claude-sonnet-4-6")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref="session-42",
            continue_chat_id="c42",
        )

    with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
        run_primary_launch(
            project_root=project_root,
            continue_ref="p42",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    request = captured["request"]
    assert request.launch_policy_snapshot is None


def test_primary_continue_snapshot_replay_preserves_agent_over_config_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent A at launch must survive --continue after config default moves to agent B."""

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".mars").mkdir()
    (project_root / ".mars" / "config.toml").write_text(
        '[primary]\nagent = "agent-b"\n',
        encoding="utf-8",
    )
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="gpt-5.3-codex",
        harness="codex",
        agent="agent-a",
        skills=(),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p43",
        harness_session_id="session-43",
        launch_policy_snapshot=snapshot,
    )

    bundle_calls: list[object] = []

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        bundle_calls.append(True)
        raise AssertionError("snapshot replay should not call mars launch-bundle")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    launch_request = LaunchRequest(
        harness="codex",
        session_mode=SessionMode.RESUME,
        launch_policy_snapshot=snapshot,
        session=SessionRequest(
            requested_harness_session_id="session-43",
            continue_harness="codex",
        ),
    )
    spawn_request = build_primary_spawn_request(request=launch_request)
    runtime = build_primary_launch_runtime(project_root=project_root)
    prepared_policy = compile_prepared_policy_surface(
        request=spawn_request,
        runtime=runtime,
        project_root=project_root,
        harness_registry=get_default_harness_registry(),
        catalog=CatalogSession(project_root),
        active_work_dir=None,
        dry_run=True,
    )

    assert bundle_calls == []
    assert prepared_policy.resolved_policy.routing.agent == "agent-a"
