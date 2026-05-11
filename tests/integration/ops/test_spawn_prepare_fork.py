from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.runtime import build_runtime_from_root_and_config
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload


def _write_minimal_subagent(project_root: Path) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "meridian-subagent.md").write_text(
        "---\n"
        "name: meridian-subagent\n"
        "description: Test subagent profile\n"
        "model: gpt-5.3-codex\n"
        "---\n"
        "\n"
        "Test profile body.\n",
        encoding="utf-8",
    )


def _prepare_codex_runtime(project_root: Path):
    _write_minimal_subagent(project_root)
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    harness_registry = get_default_harness_registry()
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    return codex_adapter, build_runtime_from_root_and_config(
        project_root, load_config(project_root)
    )


def _patch_catalog_models(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cli_model: str = "gpt-5.5",
    overlay_model: str = "claude-sonnet-4.5",
) -> None:
    cli_entry = AliasEntry(
        alias=cli_model,
        model_id=ModelId(cli_model),
        resolved_harness=HarnessId.CODEX,
    )
    overlay_entry = AliasEntry(
        alias=overlay_model,
        model_id=ModelId(overlay_model),
        resolved_harness=HarnessId.CLAUDE,
    )

    def resolve_model(self: CatalogSession, name: str) -> AliasEntry:
        return {
            cli_model: cli_entry,
            overlay_model: overlay_entry,
        }[name]

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [cli_entry, overlay_entry])


def test_fork_prepare_preserves_continue_fork_and_defers_materialization(
    monkeypatch, tmp_path: Path
) -> None:
    """I-10: build_create_payload must NOT call fork_session.

    Fork materialization is deferred to execute.py (after the spawn row exists).
    prepare.py's job is to preserve continue_fork=True so the executor can act on it.
    """
    codex_adapter, runtime = _prepare_codex_runtime(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        codex_adapter,
        "fork_session",
        lambda source_session_id: calls.append(source_session_id) or "forked-session",
    )

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="fork prompt",
            project_root=tmp_path.as_posix(),
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_harness="codex",
                continue_fork=True,
            ),
            dry_run=False,
        ),
        runtime=runtime,
    )
    dry_run_prepared = build_create_payload(
        SpawnCreateInput(
            prompt="fork prompt",
            project_root=tmp_path.as_posix(),
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_harness="codex",
                continue_fork=True,
            ),
            dry_run=True,
        ),
        runtime=runtime,
    )

    # I-10: fork_session must NOT be called in prepare — fork happens after the row exists.
    assert calls == []
    # The source session ID and continue_fork=True are preserved for the executor.
    assert prepared.session.requested_harness_session_id == "source-session"
    assert prepared.session.continue_fork is True
    # dry_run also preserves the deferred state.
    assert dry_run_prepared.session.requested_harness_session_id == "source-session"
    assert dry_run_prepared.session.continue_fork is True

    dry_run_command = " ".join(dry_run_prepared.cli_command)
    assert "/spawns/preview/report.md" not in dry_run_command
    assert "<spawn-report-path>" in dry_run_command


def test_build_create_payload_returns_durable_spawn_request_without_prepared_surface_fields(
    tmp_path: Path,
) -> None:
    _write_minimal_subagent(tmp_path)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="test durable seam",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    )

    payload = prepared.model_dump(mode="json", exclude_none=True)

    assert payload["prompt"] == "test durable seam"
    assert "cli_command" in payload
    for prepared_only_field in (
        "seed_harness_session_id",
        "agent_inventory_prompt",
        "context_prompt",
        "alias_catalog",
    ):
        assert prepared_only_field not in payload


def test_build_create_payload_applies_agent_overlay_layering_and_per_field_cli_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_subagent(tmp_path)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.toml").write_text(
        '[agents.meridian-subagent]\nmodel = "claude-sonnet-4.5"\neffort = "medium"\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.local.toml").write_text(
        '[agents.meridian-subagent]\neffort = "high"\n',
        encoding="utf-8",
    )
    user_config = tmp_path / "user-config.toml"
    user_config.write_text(
        '[agents.meridian-subagent]\neffort = "low"\n',
        encoding="utf-8",
    )
    _patch_catalog_models(monkeypatch)
    runtime = build_runtime_from_root_and_config(
        tmp_path,
        load_config(tmp_path, user_config=user_config),
    )

    overlay_routed = build_create_payload(
        SpawnCreateInput(
            prompt="overlay routing",
            project_root=tmp_path.as_posix(),
            agent="meridian-subagent",
            dry_run=True,
        ),
        runtime=runtime,
    )
    cli_model_overridden = build_create_payload(
        SpawnCreateInput(
            prompt="cli beats overlay model",
            project_root=tmp_path.as_posix(),
            agent="meridian-subagent",
            model="gpt-5.5",
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert overlay_routed.model == "claude-sonnet-4.5"
    assert overlay_routed.harness == "claude"
    assert overlay_routed.execution_policy.effort == "high"

    assert cli_model_overridden.model == "gpt-5.5"
    assert cli_model_overridden.harness == "codex"
    assert cli_model_overridden.execution_policy.effort == "high"
    assert cli_model_overridden.model_selection_requested_token == "gpt-5.5"
    assert cli_model_overridden.model_selection_canonical_id == "gpt-5.5"
    assert cli_model_overridden.model_selection_harness_provenance == "mars-provided"


def test_build_create_payload_ignores_agent_overlays_when_no_agent_is_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_subagent(tmp_path)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.toml").write_text(
        '[agents.meridian-subagent]\nmodel = "claude-sonnet-4.5"\neffort = "high"\n',
        encoding="utf-8",
    )
    _patch_catalog_models(monkeypatch)
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="no agent means no overlay",
            project_root=tmp_path.as_posix(),
            model="gpt-5.5",
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert prepared.agent is None
    assert prepared.model == "gpt-5.5"
    assert prepared.harness == "codex"
    assert prepared.execution_policy.effort is None


def test_build_create_payload_carries_goal_from_spawn_create_input(tmp_path: Path) -> None:
    _write_minimal_subagent(tmp_path)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="compose with completion contract",
            goal="ship phase-2 gate fixes",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert prepared.goal == "ship phase-2 gate fixes"
