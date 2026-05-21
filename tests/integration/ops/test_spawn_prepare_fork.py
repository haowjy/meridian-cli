from dataclasses import dataclass
from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.runtime import build_runtime_from_root_and_config
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload


@dataclass(frozen=True)
class _FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()


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
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    harness_registry = get_default_harness_registry()
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    return codex_adapter, build_runtime_from_root_and_config(
        project_root, load_config(project_root)
    )


def _stub_bundle_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    model_routes = {
        "claude-sonnet-4.5": ("claude-sonnet-4.5", HarnessId.CLAUDE),
        "gpt-5.5": ("gpt-5.5", HarnessId.CODEX),
        "gpt-5.4-mini": ("gpt-5.4-mini", HarnessId.CODEX),
    }

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        selected_model, selected_harness = model_routes.get(
            request.model_override or "",
            ("gpt-5.3-codex", HarnessId.CODEX),
        )
        return _FakeBundleResult(
            model=selected_model,
            model_token=request.model_override or selected_model,
            harness=selected_harness,
            harness_model=selected_model,
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)


def test_build_create_payload_does_not_forward_meridian_primary_or_legacy_defaults_to_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude", ".codex", ".opencode"]\n'
        'default_model = "mars-default-model"\n'
        'default_harness = "opencode"\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.toml").write_text(
        "[defaults]\n"
        'model = "legacy-default-model"\n'
        'harness = "claude"\n'
        "\n"
        "[primary]\n"
        'model = "primary-model"\n'
        'harness = "codex"\n',
        encoding="utf-8",
    )
    captured_requests: list[bundle_adapter.BundleRequest] = []

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured_requests.append(request)
        return _FakeBundleResult(
            model="mars-default-model",
            model_token="mars-default-model",
            harness=HarnessId.OPENCODE,
            harness_model="openai/mars-default-model",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "project", "harness_source": "project"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="use mars project routing defaults",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.model_override is None
    assert request.harness_override is None
    assert prepared.model == "mars-default-model"
    assert prepared.harness == "opencode"


def test_fork_prepare_preserves_continue_fork_and_defers_materialization(
    monkeypatch, tmp_path: Path
) -> None:
    """I-10: build_create_payload must NOT call fork_session.

    Fork materialization is deferred to execute.py (after the spawn row exists).
    prepare.py's job is to preserve continue_fork=True so the executor can act on it.
    """
    codex_adapter, runtime = _prepare_codex_runtime(tmp_path)
    _stub_bundle_adapter(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        codex_adapter,
        "fork_session",
        lambda source_session_id: calls.append(source_session_id) or "forked-session",
    )

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="fork prompt",
            model="gpt-5.4-mini",
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
            model="gpt-5.4-mini",
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
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="test durable seam",
            model="gpt-5.4-mini",
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

def test_build_create_payload_carries_goal_from_spawn_create_input(tmp_path: Path) -> None:
    _write_minimal_subagent(tmp_path)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="compose with completion contract",
            goal="ship phase-2 gate fixes",
            model="gpt-5.4-mini",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert prepared.goal == "ship phase-2 gate fixes"
