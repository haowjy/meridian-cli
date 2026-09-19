"""OpenCode V2 native permission/config projection (probe-backed shapes)."""

from __future__ import annotations

import json
import logging

import pytest

from meridian.lib.harness.projections.project_opencode_streaming import (
    HarnessCapabilityMismatch,
    merge_opencode_v2_permission_config,
    project_opencode_v2_permissions,
)


def test_project_opencode_v2_permissions_maps_aliases_and_scoped_resources() -> None:
    rules = project_opencode_v2_permissions(
        json.dumps(
            {
                "*": "deny",
                "bash": "allow",
                "bash(git status)": "allow",
                "write": "allow",
                "task": "deny",
                "websearch(foo)": "ask",
            }
        )
    )

    assert rules == [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "shell", "resource": "*", "effect": "allow"},
        {"action": "shell", "resource": "git status", "effect": "allow"},
        {"action": "edit", "resource": "*", "effect": "allow"},
        {"action": "subagent", "resource": "*", "effect": "deny"},
        {"action": "websearch", "resource": "foo", "effect": "ask"},
    ]


def test_project_opencode_v2_permissions_empty_when_no_override() -> None:
    assert project_opencode_v2_permissions(None) == []
    assert project_opencode_v2_permissions("") == []
    assert project_opencode_v2_permissions("   ") == []


def test_project_opencode_v2_permissions_rejects_bad_effect() -> None:
    with pytest.raises(HarnessCapabilityMismatch, match="effect"):
        project_opencode_v2_permissions(json.dumps({"bash": "sometimes"}))


def test_merge_returns_raw_unchanged_without_policy() -> None:
    raw = json.dumps({"permission": {"external_directory": {"/tmp/**": "allow"}}})
    assert merge_opencode_v2_permission_config(raw, None) == raw
    assert merge_opencode_v2_permission_config(None, None) is None


def test_merge_emits_native_permissions_and_preserves_other_config() -> None:
    raw = json.dumps({"model": "openai/gpt-6-astra", "theme": "native"})
    merged = merge_opencode_v2_permission_config(
        raw, json.dumps({"*": "deny", "read": "allow"})
    )
    assert merged is not None
    assert json.loads(merged) == {
        "model": "openai/gpt-6-astra",
        "theme": "native",
        "permissions": [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "read", "resource": "*", "effect": "allow"},
        ],
    }


def test_merge_places_workspace_grants_last_after_broad_deny() -> None:
    raw = json.dumps(
        {
            "permission": {
                "external_directory": {"/repo/**": "allow", "/tmp/**": "allow"}
            }
        }
    )
    merged = merge_opencode_v2_permission_config(raw, json.dumps({"*": "deny"}))
    assert merged is not None
    payload = json.loads(merged)
    assert "permission" not in payload
    assert payload["permissions"] == [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": "/repo/**", "effect": "allow"},
        {"action": "external_directory", "resource": "/tmp/**", "effect": "allow"},
    ]


def test_merge_keeps_existing_native_permissions_before_tools_policy() -> None:
    raw = json.dumps(
        {"permissions": [{"action": "read", "resource": "*.env", "effect": "deny"}]}
    )
    merged = merge_opencode_v2_permission_config(raw, json.dumps({"read": "allow"}))
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "read", "resource": "*.env", "effect": "deny"},
        {"action": "read", "resource": "*", "effect": "allow"},
    ]


def test_merge_collapses_within_policy_alias_collision_to_deny() -> None:
    merged = merge_opencode_v2_permission_config(
        None, json.dumps({"edit": "deny", "write": "allow"})
    )
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "edit", "resource": "*", "effect": "deny"}
    ]


def test_merge_collapses_patch_alias_collision_to_deny() -> None:
    merged = merge_opencode_v2_permission_config(
        None, json.dumps({"edit": "allow", "patch": "deny"})
    )
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "edit", "resource": "*", "effect": "deny"}
    ]


def test_merge_warns_on_effect_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="meridian.lib.harness.projections.project_opencode_streaming",
    ):
        merge_opencode_v2_permission_config(
            None, json.dumps({"edit": "deny", "write": "allow"})
        )
    assert "collision" in caplog.text


def test_merge_inherited_non_root_does_not_shadow_tools_deny() -> None:
    raw = json.dumps({"permission": {"edit": "allow"}})
    merged = merge_opencode_v2_permission_config(raw, json.dumps({"edit": "deny"}))
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "edit", "resource": "*", "effect": "allow"},
        {"action": "edit", "resource": "*", "effect": "deny"},
    ]


def test_merge_inherited_broad_allow_does_not_shadow_broad_deny() -> None:
    raw = json.dumps({"permission": {"*": "allow"}})
    merged = merge_opencode_v2_permission_config(raw, json.dumps({"*": "deny"}))
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "*", "resource": "*", "effect": "allow"},
        {"action": "*", "resource": "*", "effect": "deny"},
    ]


def test_merge_inherited_external_directory_grant_stays_last_after_broad_deny() -> None:
    raw = json.dumps(
        {
            "permission": {
                "*": "allow",
                "external_directory": {"/repo/**": "allow", "/tmp/**": "allow"},
            }
        }
    )
    merged = merge_opencode_v2_permission_config(raw, json.dumps({"*": "deny"}))
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "*", "resource": "*", "effect": "allow"},
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": "/repo/**", "effect": "allow"},
        {"action": "external_directory", "resource": "/tmp/**", "effect": "allow"},
    ]


def test_merge_scoped_exception_refines_broad_deny() -> None:
    merged = merge_opencode_v2_permission_config(
        None, json.dumps({"edit": "deny", "edit(foo)": "allow"})
    )
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "foo", "effect": "allow"},
    ]


def test_merge_meridian_allow_wins_over_inherited_non_root_deny() -> None:
    raw = json.dumps({"permission": {"edit": "deny"}})
    merged = merge_opencode_v2_permission_config(raw, json.dumps({"edit": "allow"}))
    assert merged is not None
    assert json.loads(merged)["permissions"] == [
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "*", "effect": "allow"},
    ]

