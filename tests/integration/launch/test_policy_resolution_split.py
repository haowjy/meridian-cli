"""Policy resolution and prepared-surface assembly stay separate."""

from pathlib import Path

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import context
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from tests.support.launch import stub_bundle_request_and_resolve


def test_prepared_surface_uses_the_exact_policy_resolved_before_late_inputs_change(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "meridian.toml"
    config_file.write_text("[primary]\nagent = 'first'\n")
    captured = stub_bundle_request_and_resolve(
        monkeypatch, model="gpt-5", harness=HarnessId.CODEX
    )
    request = SpawnRequest(prompt="test", agent_opt_out=True)
    runtime = LaunchRuntime(
        composition_surface=LaunchCompositionSurface.PRIMARY,
        runtime_root=str(tmp_path / ".meridian"),
        project_paths_project_root=str(tmp_path),
        project_paths_execution_cwd=str(tmp_path),
    )
    catalog = CatalogSession(tmp_path)

    policy = context.resolve_launch_policy_for_request(
        request=request,
        runtime=runtime,
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        catalog=catalog,
        dry_run=True,
    )
    assert len(captured) == 1

    # Late packaging cannot consult policy inputs again, even if they changed.
    config_file.write_text("[primary]\nagent = 'second'\n")
    monkeypatch.setattr(
        context,
        "resolve_launch_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rerouted policy")),
    )
    prepared = context.assemble_prepared_policy_surface(
        resolved_policy=policy,
        runtime=runtime,
        project_root=tmp_path,
        active_work_dir=tmp_path / "work",
    )

    assert prepared.resolved_policy is policy
    assert prepared.resolved_policy.model == "gpt-5"
    assert prepared.resolved_policy.harness == HarnessId.CODEX


def test_compiler_retains_paths_resolved_before_policy_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "meridian.toml"
    config_file.write_text("[primary]\nagent = 'first'\n")
    task_a = tmp_path / "task-a"
    task_b = tmp_path / "task-b"
    task_a.mkdir()
    task_b.mkdir()
    task_alias = tmp_path / "task"
    task_alias.symlink_to(task_a, target_is_directory=True)
    stub_bundle_request_and_resolve(
        monkeypatch, model="gpt-5", harness=HarnessId.CODEX
    )
    request = SpawnRequest(prompt="test", agent_opt_out=True)
    runtime = LaunchRuntime(
        composition_surface=LaunchCompositionSurface.PRIMARY,
        runtime_root=str(tmp_path / ".meridian"),
        project_paths_project_root=str(tmp_path),
        project_paths_execution_cwd=str(task_alias),
    )

    resolve_policy = context.resolve_launch_policy

    def resolve_then_retarget(*args, **kwargs):
        policy = resolve_policy(*args, **kwargs)
        task_alias.unlink()
        task_alias.symlink_to(task_b, target_is_directory=True)
        return policy

    monkeypatch.setattr(context, "resolve_launch_policy", resolve_then_retarget)
    prepared = context.compile_prepared_policy_surface(
        request=request,
        runtime=runtime,
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        catalog=CatalogSession(tmp_path),
        active_work_dir=tmp_path / "work",
        dry_run=True,
    )

    assert prepared.project_paths.execution_cwd == task_a.resolve()
