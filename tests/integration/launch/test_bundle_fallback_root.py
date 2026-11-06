"""Fallback-root transport and bundle source provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog import mars_roots
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.bundle_adapter import BundleRequest
from meridian.lib.launch.context import compile_prepared_policy_surface
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest


@pytest.mark.parametrize(
    ("project_scope", "supported", "expects_fallback"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_bundle_fallback_flag_requires_project_scope_and_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_scope: bool,
    supported: bool,
    expects_fallback: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    if project_scope:
        (project / "mars.toml").write_text("", encoding="utf-8")
    user_home = tmp_path / "home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(
        mars_roots,
        "mars_supports_fallback_root",
        lambda _executable: supported,
    )

    command = bundle_adapter._build_bundle_command(BundleRequest(agent=None, project_root=project))

    fallback_args = ["--fallback-root", (user_home / "mars").resolve().as_posix()]
    if expects_fallback:
        assert command[-2:] == fallback_args
    else:
        assert "--fallback-root" not in command


def test_failed_bundle_explains_fallback_agent_alias_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    user_home = tmp_path / "home"
    user_root = user_home / "mars"
    agents_dir = user_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "explorer.md").write_text(
        "---\nname: explorer\nmodel: luna\n---\n\nExplore.\n",
        encoding="utf-8",
    )
    fake_mars = tmp_path / "mars"
    fake_mars.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        '\'{"error":{"code":"model_candidates_exhausted",'
        '"message":"model fallback candidates exhausted for `luna`"}}\'\n'
        "exit 2\n",
        encoding="utf-8",
    )
    fake_mars.chmod(0o755)
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: str(fake_mars))
    monkeypatch.setattr(
        mars_roots,
        "mars_supports_fallback_root",
        lambda _executable: True,
    )

    with pytest.raises(RuntimeError) as caught:
        compile_prepared_policy_surface(
            request=SpawnRequest(prompt="test", agent="explorer"),
            runtime=LaunchRuntime(
                composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
                runtime_root=str(tmp_path / "runtime"),
                project_paths_project_root=str(project),
                project_paths_execution_cwd=str(project),
            ),
            project_root=project,
            harness_registry=get_default_harness_registry(),
            catalog=CatalogSession(project),
            dry_run=True,
        )

    assert str(caught.value) == (
        "model fallback candidates exhausted for `luna`\n"
        f"note: 'explorer' comes from user packages ({user_root.resolve()}); "
        "its model aliases must resolve in this project's Mars config. "
        "Add the package here (meridian mars add <source>) or pass --model."
    )
