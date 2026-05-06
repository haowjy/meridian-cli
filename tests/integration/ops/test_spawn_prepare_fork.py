from pathlib import Path

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
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
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )
    harness_registry = get_default_harness_registry()
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    return codex_adapter, build_runtime_from_root_and_config(
        project_root, load_config(project_root)
    )


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
        "[settings]\n"
        'targets = [".claude"]\n',
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
