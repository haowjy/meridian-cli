"""Headless policy narrows Mars selection before a harness is chosen."""

from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.context import compile_prepared_policy_surface
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from tests.support.launch import stub_bundle_request_and_resolve


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        (LaunchCompositionSurface.PRIMARY, ()),
        (LaunchCompositionSurface.SPAWN_PREPARE, (HarnessId.CLAUDE, HarnessId.PI)),
    ],
)
def test_caller_exclusions_reach_mars_before_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: LaunchCompositionSurface,
    expected: tuple[HarnessId, ...],
) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[spawn]\ndeny_headless_harnesses = ["claude", "pi", "claude"]\n'
    )
    captured = stub_bundle_request_and_resolve(monkeypatch, model="gpt-5", harness=HarnessId.CODEX)
    compile_prepared_policy_surface(
        request=SpawnRequest(prompt="test", agent_opt_out=True),
        runtime=LaunchRuntime(
            composition_surface=surface,
            runtime_root=str(tmp_path / ".meridian"),
            project_paths_project_root=str(tmp_path),
            project_paths_execution_cwd=str(tmp_path),
        ),
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        catalog=CatalogSession(tmp_path),
        dry_run=True,
    )

    assert len(captured) == 1
    assert captured[0].excluded_harnesses == expected


def test_exclusions_are_transported_as_repeated_harness_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_mars = tmp_path / "mars"
    argv_log = tmp_path / "argv.log"
    fake_mars.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_LOG"\n'
        'echo "fixture stops after recording arguments" >&2\nexit 2\n'
    )
    fake_mars.chmod(0o755)
    monkeypatch.setenv("ARGV_LOG", str(argv_log))
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: str(fake_mars))
    request = bundle_adapter.BundleRequest(
        agent=None,
        project_root=tmp_path,
        model_override="gpt-5",
        harness_override="codex",
        excluded_harnesses=(HarnessId.CLAUDE, HarnessId.PI),
    )
    with pytest.raises(RuntimeError, match="fixture stops after recording arguments"):
        bundle_adapter.request_and_resolve(request, harness_registry=get_default_harness_registry())
    assert argv_log.read_text().splitlines() == [
        "build",
        "launch-bundle",
        "--json",
        "--root",
        str(tmp_path),
        "--model",
        "gpt-5",
        "--harness",
        "codex",
        "--exclude-harness",
        "claude",
        "--exclude-harness",
        "pi",
    ]


@pytest.mark.parametrize("harness", [None, "codex"])
def test_public_env_model_and_cli_harness_pin_independent_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str | None
) -> None:
    monkeypatch.setenv("MERIDIAN_MODEL", "gpt-5")
    monkeypatch.setenv("MERIDIAN_HARNESS", "claude")
    monkeypatch.setenv("_MERIDIAN_HARNESS", "claude")
    captured = stub_bundle_request_and_resolve(monkeypatch, model="gpt-5", harness=HarnessId.CODEX)
    compile_prepared_policy_surface(
        request=SpawnRequest(prompt="test", harness=harness, agent_opt_out=True),
        runtime=LaunchRuntime(
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=str(tmp_path / ".meridian"),
            project_paths_project_root=str(tmp_path),
            project_paths_execution_cwd=str(tmp_path),
        ),
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        catalog=CatalogSession(tmp_path),
        dry_run=True,
    )
    assert captured[0].model_override == "gpt-5"
    assert captured[0].harness_override == harness
