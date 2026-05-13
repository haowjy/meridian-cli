# qa-validated: test-suite-redesign
"""Chat CLI policy snapshot resolution tests — model alias, harness, agent, skills, approval."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from meridian.cli import chat_cmd
from meridian.cli.chat_cmd import run_chat_server
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.chat.policy import build_chat_backend_launch_plan, default_chat_policy_snapshot
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from tests.support.fixtures import write_skill


class EmptyPipelineLookup:
    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot or default_chat_policy_snapshot()

    def get_pipeline(self, chat_id: str):
        _ = chat_id
        return None

    def get_policy_snapshot(self, chat_id: str):
        _ = chat_id
        return self._snapshot


def _mock_alias(alias: str, model_id: str, harness: HarnessId) -> AliasEntry:
    return AliasEntry(alias=alias, model_id=ModelId(model_id), resolved_harness=harness)


# Note: no _stable_policy_resolution fixture here — these tests exercise real policy resolution.


def test_chat_policy_resolution_fails_before_runtime_configure_or_discovery_write(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    configured: list[object] = []
    write_bootstrap_calls: list[Path] = []
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(chat_cmd, "require_established_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "prepare_for_runtime_write",
        lambda root: write_bootstrap_calls.append(root),
    )
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_chat_policy_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("policy-failed")),
    )

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(chat_server, "app", object())

    with pytest.raises(ValueError, match="policy-failed"):
        run_chat_server(
            port=8765,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )

    assert configured == []
    assert write_bootstrap_calls == []
    assert not (runtime_root / "chat-server.json").exists()


def test_chat_policy_snapshot_resolves_alias_to_canonical_model(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("codex", "gpt-5.3-codex", HarnessId.CODEX)

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"codex", "gpt-5.3-codex"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="codex",
        harness="codex",
        agent=None,
        skills=(),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.canonical_model_id == "gpt-5.3-codex"
    assert snapshot.harness == "codex"


def test_chat_policy_snapshot_rejects_incompatible_explicit_harness(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    with pytest.raises(ValueError, match="incompatible"):
        chat_cmd._resolve_chat_policy_snapshot(
            project_root=tmp_path,
            model="gptmini",
            harness="claude",
            agent=None,
            skills=(),
            approval=None,
            sandbox=None,
            effort=None,
            autocompact=None,
        )


def test_chat_policy_snapshot_without_model_does_not_force_catalog_lookup(
    monkeypatch, tmp_path
) -> None:
    calls: list[str] = []

    def fail_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        calls.append(f"resolve:{token}")
        raise AssertionError("catalog resolve_model should not run without a model token")

    def fail_load_aliases(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        calls.append("load_aliases")
        raise AssertionError("catalog load_aliases should not run without a model token")

    monkeypatch.setattr(CatalogSession, "resolve_model", fail_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", fail_load_aliases)

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model=None,
        harness=None,
        agent=None,
        skills=(),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert calls == []
    assert snapshot.harness == "claude"
    assert snapshot.canonical_model_id == ""


def test_chat_policy_snapshot_collects_profile_and_missing_skill_warnings(tmp_path) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[primary]\nagent = "missing-default"\n',
        encoding="utf-8",
    )

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model=None,
        harness=None,
        agent=None,
        skills=("missing-skill",),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    warning_codes = [warning.code for warning in snapshot.warnings]
    assert "profile_warning" in warning_codes
    assert "missing_skills_warning" in warning_codes
    warning_messages = [warning.message for warning in snapshot.warnings]
    assert any(
        "Configured agent profile 'missing-default' is unavailable" in message
        for message in warning_messages
    )
    assert any("Warning: Skipped unavailable skills: missing-skill" in m for m in warning_messages)


def test_chat_policy_snapshot_explicit_missing_agent_fails_before_startup(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    configured: list[object] = []
    write_bootstrap_calls: list[Path] = []
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(chat_cmd, "require_established_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "prepare_for_runtime_write",
        lambda root: write_bootstrap_calls.append(root),
    )

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(chat_server, "app", object())

    with pytest.raises(FileNotFoundError, match="missing-explicit-agent"):
        run_chat_server(
            agent="missing-explicit-agent",
            port=8765,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )

    assert configured == []
    assert write_bootstrap_calls == []
    assert not (runtime_root / "chat-server.json").exists()


def test_chat_policy_snapshot_loads_harness_and_model_skill_variant(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)
    write_skill(tmp_path, "variant-skill", body="Base body")
    token_variant = (
        tmp_path
        / ".mars"
        / "skills"
        / "variant-skill"
        / "variants"
        / "codex"
        / "gptmini"
        / "SKILL.md"
    )
    token_variant.parent.mkdir(parents=True, exist_ok=True)
    token_variant.write_text("Token variant body\n", encoding="utf-8")
    canonical_variant = (
        tmp_path
        / ".mars"
        / "skills"
        / "variant-skill"
        / "variants"
        / "codex"
        / "gpt-5.4-mini"
        / "SKILL.md"
    )
    canonical_variant.parent.mkdir(parents=True, exist_ok=True)
    canonical_variant.write_text("Canonical variant body\n", encoding="utf-8")

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="gptmini",
        harness="codex",
        agent=None,
        skills=("variant-skill",),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.skills == ("variant-skill",)
    assert snapshot.prompt_inputs.skill_documents == ()


def test_chat_policy_snapshot_approval_env_beats_profile_and_config(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    (tmp_path / "meridian.toml").write_text(
        '[primary]\napproval = "auto"\n',
        encoding="utf-8",
    )
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (tmp_path / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mars" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\napproval: yolo\n---\n\nReviewer profile body.\n",
        encoding="utf-8",
    )

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="gptmini",
        harness=None,
        agent="reviewer",
        skills=(),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.execution_policy.approval == "confirm"
    assert snapshot.field_provenance["approval"] == "env"


def test_chat_policy_snapshot_with_agent_and_cli_overrides_feeds_launch_plan(
    monkeypatch, tmp_path
) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)
    write_skill(tmp_path, "profile-skill", body="Profile skill body")
    write_skill(tmp_path, "cli-skill", body="CLI skill body")
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (tmp_path / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mars" / "agents" / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "model: claude-sonnet-4-6\n"
        "approval: auto\n"
        "autocompact: 200000\n"
        "sandbox: workspace-write\n"
        "skills:\n"
        "  - profile-skill\n"
        "tools:\n"
        "  '*': allow\n"
        "  read: allow\n"
        "  bash: deny\n"
        "mcp-tools:\n"
        "  - github=gh\n"
        "model-policies:\n"
        "  - match:\n"
        "      model: gpt-5.4-mini\n"
        "    override:\n"
        "      effort: low\n"
        "---\n\n"
        "Reviewer profile body.\n",
        encoding="utf-8",
    )

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="gptmini",
        harness=None,
        agent="reviewer",
        skills=("cli-skill", "profile-skill"),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.requested_model_token == "gptmini"
    assert snapshot.selected_model_token == "gptmini"
    assert snapshot.canonical_model_id == "gpt-5.4-mini"
    assert snapshot.harness == "codex"
    assert snapshot.agent_name == "reviewer"
    assert snapshot.execution_policy.approval == "auto"
    assert snapshot.execution_policy.sandbox == "workspace-write"
    assert snapshot.execution_policy.effort == "low"
    assert snapshot.execution_policy.autocompact == 200000
    assert snapshot.skills == ("profile-skill", "cli-skill")
    assert snapshot.tools == {"*": "allow", "read": "allow", "bash": "deny"}
    assert snapshot.mcp_tools == ("github=gh",)
    assert snapshot.prompt_inputs.agent_profile_body == ""
    assert snapshot.prompt_inputs.adhoc_agent_payload == "Reviewer profile body."
    assert snapshot.prompt_inputs.skill_documents == ()

    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId.CODEX)
    plan = build_chat_backend_launch_plan(
        snapshot=snapshot,
        initial_prompt="Please review this change.",
        spawn_id=SpawnId("chat-test"),
        adapter=adapter,
        project_root=tmp_path,
        runtime_root=tmp_path / "runtime",
    )

    assert plan.harness_id == HarnessId.CODEX
    assert plan.connection_config.harness_id == HarnessId.CODEX
    assert isinstance(plan.spec, ResolvedLaunchSpec)
    assert plan.spec.model == "gpt-5.4-mini"
    assert plan.spec.base_instructions == "Reviewer profile body."
    assert plan.spec.user_turn_content == "Please review this change."
    assert "Profile skill body" not in (plan.spec.developer_instructions or "")
    assert "CLI skill body" not in (plan.spec.developer_instructions or "")
    assert plan.spec.mcp_tools == ("github=gh",)
    assert not isinstance(plan.spec.permission_resolver, UnsafeNoOpPermissionResolver)
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in plan.connection_config.env_overrides
    assert plan.configured_payload == {
        "harness": "codex",
        "model": "gpt-5.4-mini",
        "requested_model_token": "gptmini",
        "selected_model_token": "gptmini",
        "harness_provenance": "mars-provided",
        "policy_snapshot_id": snapshot.snapshot_id,
    }
