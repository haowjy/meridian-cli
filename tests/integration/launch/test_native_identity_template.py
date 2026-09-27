"""Native identity templates fail before exec and keep fork stores independent."""

import json
from pathlib import Path

import pytest

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.claude_sessions import project_slug
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from tests.support.executables import prepend_fake_executables
from tests.support.launch import stub_bundle_request_and_resolve


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("source_id", ["missing-session", "../x"])
def test_claude_dry_run_refuses_missing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracked: bool,
    source_id: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    stub_bundle_request_and_resolve(monkeypatch, model="test-model", harness=HarnessId.CLAUDE)
    prepend_fake_executables(monkeypatch, tmp_path, "claude")
    if source_id == "../x":
        # A real matching file outside the store makes this a traversal test,
        # rather than another missing-file test.
        store = (
            tmp_path / "source"
            if tracked
            else (tmp_path / "claude" / "projects" / project_slug(root))
        )
        store.mkdir(parents=True)
        (store.parent / "x.jsonl").write_text(json.dumps({"sessionId": source_id}) + "\n")
    with pytest.raises(NativeSessionUnavailable, match="native_transcript_missing"):
        build_launch_context(
            spawn_id="p42",
            request=SpawnRequest(
                harness="claude",
                model="test-model",
                prompt="hello",
                session=SessionRequest(
                    requested_harness_session_id=source_id,
                    continue_source_tracked=tracked,
                    continue_source_ref="c1" if tracked else None,
                    source_native_store=str(tmp_path / "source") if tracked else None,
                ),
                launch_policy_snapshot=LaunchPolicySnapshot(model="test-model", harness="claude"),
            ),
            runtime=LaunchRuntime(
                argv_intent=LaunchArgvIntent.REQUIRED,
                composition_surface=LaunchCompositionSurface.PRIMARY,
                runtime_root=str(tmp_path / "runtime"),
                project_paths_project_root=str(root),
                project_paths_execution_cwd=str(root),
            ),
            harness_registry=HarnessRegistry.with_defaults(),
            dry_run=True,
        )


@pytest.mark.parametrize("tracked", [False, True])
def test_pi_fork_store_is_not_nested_under_source(tmp_path: Path, tracked: bool) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.PI)
    root = tmp_path / "store"
    source = root / "old-spawn" if tracked else root
    source.mkdir(parents=True)
    (source / "1_source-id.jsonl").write_text(
        json.dumps({"type": "session", "id": "source-id"}) + "\n"
    )
    intent = adapter.plan_native_identity(
        SpawnParams(
            prompt="hello",
            continue_harness_session_id="source-id",
            continue_fork=True,
        )
    )
    assert intent is not None
    identity = adapter.finalize_native_identity(
        intent,
        child_env={"PI_CODING_AGENT_SESSION_DIR": str(root)},
        child_cwd=tmp_path,
        session=SessionRequest(
            requested_harness_session_id="source-id",
            continue_fork=True,
            source_native_store=str(source) if tracked else None,
            continue_source_tracked=tracked,
        ),
        spawn_id=SpawnId("p42"),
        interactive=False,
    )
    assert identity.native_store == str(root / "p42")
    assert not (root / "p42").exists()  # Finalize does not create a store or journal.


@pytest.mark.parametrize(
    "harness,flag",
    [
        (HarnessId.CLAUDE, "--resume"),
        (HarnessId.CODEX, "--session-id"),
        (HarnessId.OPENCODE, "--session"),
        (HarnessId.PI, "--session-dir"),
    ],
)
@pytest.mark.parametrize("equals", [False, True])
def test_native_identity_refuses_passthrough(harness: HarnessId, flag: str, equals: bool) -> None:
    adapter = HarnessRegistry.with_defaults().get(harness)
    args = (f"{flag}=foreign",) if equals else (flag, "foreign")
    with pytest.raises(ValueError, match="passthrough"):
        adapter.plan_native_identity(SpawnParams(prompt="hello", extra_args=args))
