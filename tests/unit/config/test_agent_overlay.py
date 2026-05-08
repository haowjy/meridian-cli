from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.ops.config import ConfigShowInput, config_show_sync


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.delenv("MERIDIAN_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("MERIDIAN_DEFAULT_HARNESS", raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())


def _repo(tmp_path: Path, toml: str) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(toml, encoding="utf-8")
    return project_root


def test_basic_scalar_overlay_parses_from_toml(tmp_path: Path) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.coder]
model = " gptmini "
harness = "codex"
effort = "high"
approval = "auto"
sandbox = "danger-full-access"
autocompact = 80
""",
    )

    overlay = load_config(project_root).agents["coder"]

    assert overlay.model == "gptmini"
    assert overlay.harness == "codex"
    assert overlay.effort == "high"
    assert overlay.approval == "auto"
    assert overlay.sandbox == "danger-full-access"
    assert overlay.autocompact == 80
    assert overlay.model_policies is None


@pytest.mark.parametrize(
    "toml",
    [
        "[agents.coder]\neffort = 'max'\n",
        "[agents.coder]\napproval = 'sometimes'\n",
        "[agents.coder]\nautocompact = 0\n",
        "[agents.coder]\nautocompact = 101\n",
    ],
)
def test_invalid_scalar_overlay_values_raise(tmp_path: Path, toml: str) -> None:
    project_root = _repo(tmp_path, toml)

    with pytest.raises(ValueError):
        load_config(project_root)


def test_timeout_is_rejected_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.coder]
timeout = 12
model = "gptmini"
""",
    )

    config = load_config(project_root)

    assert config.agents["coder"].model == "gptmini"
    assert not hasattr(config.agents["coder"], "timeout")
    assert "agent overlays do not support timeout" in caplog.text


def test_model_policies_parse_match_and_override(tmp_path: Path) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.coder]
[[agents.coder.model-policies]]
match.alias = "gptmini"
override.harness = "codex"
override.effort = "low"
override.approval = "confirm"
override.sandbox = "workspace-write"
override.autocompact = 50
""",
    )

    policies = load_config(project_root).agents["coder"].model_policies

    assert policies is not None
    assert len(policies) == 1
    assert policies[0].match_type == "alias"
    assert policies[0].match_value == "gptmini"
    assert policies[0].overrides == {
        "harness": "codex",
        "effort": "low",
        "approval": "confirm",
        "sandbox": "workspace-write",
        "autocompact": 50,
    }


def test_model_policy_empty_override_rejected(tmp_path: Path) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.coder]
[[agents.coder.model-policies]]
match.alias = "gptmini"
override = {}
""",
    )

    with pytest.raises(ValueError, match="expected at least one override field"):
        load_config(project_root)


@pytest.mark.parametrize("key", ["skills", "tools", "disallowed-tools", "mcp-tools"])
def test_rejected_list_override_keys_warn_and_are_ignored(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    key: str,
) -> None:
    project_root = _repo(
        tmp_path,
        f"""
[agents.coder]
[[agents.coder.model-policies]]
match.model = "gpt-5.4"
override.{key} = ["thing"]
override.effort = "medium"
""",
    )

    overrides = load_config(project_root).agents["coder"].model_policies[0].overrides  # type: ignore[index]

    assert key not in overrides
    assert overrides == {"effort": "medium"}
    assert "list overrides are not supported" in caplog.text


def test_unknown_agent_fields_warn_and_are_ignored(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.unknown-agent]
model = "gptmini"
surprise = "ignored"
""",
    )

    config = load_config(project_root)

    assert config.agents["unknown-agent"].model == "gptmini"
    assert "agents.unknown-agent.surprise" in caplog.text


def test_model_policies_three_state_absent_empty_and_populated(tmp_path: Path) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.inherit]
model = "gptmini"

[agents.suppress]
model-policies = []

[agents.replace]
[[agents.replace.model-policies]]
match.model-glob = "gpt-*"
override.effort = "xhigh"
""",
    )

    agents = load_config(project_root).agents

    assert agents["inherit"].model_policies is None
    assert agents["suppress"].model_policies == ()
    assert agents["replace"].model_policies is not None
    assert len(agents["replace"].model_policies) == 1


def test_unknown_override_keys_warn_and_are_ignored(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = _repo(
        tmp_path,
        """
[agents.coder]
[[agents.coder.model-policies]]
match.alias = "codex"
override.temperature = 0.2
override.approval = "yolo"
""",
    )

    overrides = load_config(project_root).agents["coder"].model_policies[0].overrides  # type: ignore[index]

    assert overrides == {"approval": "yolo"}
    assert "agents.coder.model-policies[1].override.temperature" in caplog.text


def _write_user_config(tmp_path: Path, toml: str) -> Path:
    user_config = tmp_path / "user-config.toml"
    user_config.write_text(toml, encoding="utf-8")
    return user_config


def test_agent_overlay_layers_merge_per_field_with_project_local_winning(tmp_path: Path) -> None:
    user_config = _write_user_config(
        tmp_path,
        """
[agents.tech-lead]
model = "opus"
""",
    )
    project_root = _repo(
        tmp_path,
        """
[agents.tech-lead]
effort = "medium"
approval = "confirm"
""",
    )
    (project_root / "meridian.local.toml").write_text(
        """
[agents.tech-lead]
effort = "high"
""",
        encoding="utf-8",
    )

    overlay = load_config(project_root, user_config=user_config).agents["tech-lead"]

    assert overlay.model == "opus"
    assert overlay.effort == "high"
    assert overlay.approval == "confirm"


def test_agent_overlay_model_policies_replace_across_layers(tmp_path: Path) -> None:
    user_config = _write_user_config(
        tmp_path,
        """
[agents.tech-lead]
[[agents.tech-lead.model-policies]]
match.alias = "user-model"
override.effort = "low"
""",
    )
    project_root = _repo(
        tmp_path,
        """
[agents.tech-lead]
[[agents.tech-lead.model-policies]]
match.alias = "project-model"
override.effort = "medium"
""",
    )
    (project_root / "meridian.local.toml").write_text(
        """
[agents.tech-lead]
[[agents.tech-lead.model-policies]]
match.alias = "local-model"
override.effort = "high"
""",
        encoding="utf-8",
    )

    policies = load_config(project_root, user_config=user_config).agents["tech-lead"].model_policies

    assert policies is not None
    assert len(policies) == 1
    assert policies[0].match_value == "local-model"
    assert policies[0].overrides == {"effort": "high"}


def test_config_show_includes_agent_overlays(tmp_path: Path) -> None:
    """config show output includes agent overlay values with source=file."""
    project_root = _repo(
        tmp_path,
        """
[agents.tech-lead]
model = "gpt-5.4"
harness = "codex"
effort = "high"
approval = "auto"
sandbox = "workspace-write"
autocompact = 70

[[agents.tech-lead.model-policies]]
match.alias = "gptmini"
override.effort = "low"
""",
    )

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    values = {item.key: item for item in shown.values}

    assert values["agents.tech-lead.model"].value == "gpt-5.4"
    assert values["agents.tech-lead.model"].source == "file"
    assert values["agents.tech-lead.harness"].value == "codex"
    assert values["agents.tech-lead.effort"].value == "high"
    assert values["agents.tech-lead.approval"].value == "auto"
    assert values["agents.tech-lead.sandbox"].value == "workspace-write"
    assert values["agents.tech-lead.autocompact"].value == 70
    assert values["agents.tech-lead.model-policies"].source == "file"
    assert values["agents.tech-lead.model-policies"].value == (
        '1 rules (match: alias "gptmini" → effort=low)'
    )


def test_empty_agent_overlay_model_policies_suppress_weaker_entries(tmp_path: Path) -> None:
    user_config = _write_user_config(
        tmp_path,
        """
[agents.tech-lead]
[[agents.tech-lead.model-policies]]
match.alias = "user-model"
override.effort = "low"
""",
    )
    project_root = _repo(
        tmp_path,
        """
[agents.tech-lead]
model-policies = []
""",
    )

    overlay = load_config(project_root, user_config=user_config).agents["tech-lead"]

    assert overlay.model_policies == ()


def test_runtime_overrides_extract_agent_overlay_routing_fields(tmp_path: Path) -> None:
    from meridian.lib.core.overrides import RuntimeOverrides

    project_root = _repo(
        tmp_path,
        """
[agents.coder]
model = "gptmini"
harness = "codex"
effort = "high"
approval = "auto"
sandbox = "danger-full-access"
autocompact = 80
""",
    )
    overlay = load_config(project_root).agents["coder"]

    overrides = RuntimeOverrides.from_agent_overlay_routing(overlay)

    assert overrides.model == "gptmini"
    assert overrides.harness == "codex"
    assert overrides.agent is None
    assert overrides.effort is None
    assert overrides.approval is None
    assert overrides.sandbox is None
    assert overrides.autocompact is None
    assert RuntimeOverrides.from_agent_overlay_routing(None) == RuntimeOverrides()


def test_runtime_overrides_extract_agent_overlay_policy_fields(tmp_path: Path) -> None:
    from meridian.lib.core.overrides import RuntimeOverrides

    project_root = _repo(
        tmp_path,
        """
[agents.coder]
model = "gptmini"
harness = "codex"
effort = "high"
approval = "auto"
sandbox = "danger-full-access"
autocompact = 80
""",
    )
    overlay = load_config(project_root).agents["coder"]

    overrides = RuntimeOverrides.from_agent_overlay_policy(overlay)

    assert overrides.model is None
    assert overrides.harness is None
    assert overrides.agent is None
    assert overrides.effort == "high"
    assert overrides.approval == "auto"
    assert overrides.sandbox == "danger-full-access"
    assert overrides.autocompact == 80
    assert RuntimeOverrides.from_agent_overlay_policy(None) == RuntimeOverrides()


def test_config_snapshot_round_trip_preserves_agent_overlay_data(tmp_path: Path) -> None:
    from meridian.lib.config.settings import MeridianConfig

    project_root = _repo(
        tmp_path,
        """
[agents.coder]
model = "gptmini"
harness = "codex"
effort = "high"
approval = "auto"
sandbox = "danger-full-access"
autocompact = 80

[[agents.coder.model-policies]]
match.alias = "gptmini"
override.effort = "low"
override.approval = "confirm"
""",
    )

    snapshot = load_config(project_root).model_dump(mode="json", exclude_none=True)
    restored = MeridianConfig.model_validate(snapshot)
    overlay = restored.agents["coder"]

    assert snapshot["agents"]["coder"]["model"] == "gptmini"  # type: ignore[index]
    assert overlay.model == "gptmini"
    assert overlay.harness == "codex"
    assert overlay.effort == "high"
    assert overlay.approval == "auto"
    assert overlay.sandbox == "danger-full-access"
    assert overlay.autocompact == 80
    assert overlay.model_policies is not None
    assert len(overlay.model_policies) == 1
    assert overlay.model_policies[0].match_type == "alias"
    assert overlay.model_policies[0].match_value == "gptmini"
    assert overlay.model_policies[0].overrides == {"effort": "low", "approval": "confirm"}
