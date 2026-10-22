"""Build the one version-native OpenCode launch config at bind.

Every launch owns a reserved config agent. Its optional prompt, ordered permission policy
and workspace grants are emitted together, so V1 subprocesses and V2 HTTP sessions inherit
the same bind-time decision and select the same agent. The agent carries no ``model``: V2
ignores it, so the model remains a top-level config value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from meridian.lib.harness.opencode_backend import OpenCodeVersion
from meridian.lib.harness.projections.permissions_opencode import (
    OpenCodePermissionRule,
    OpenCodePermissions,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch

OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"

_AGENT_ID_PREFIX = "meridian"
_UNSAFE_AGENT_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
# OpenCode substitutes ``{file:<path>}`` on the raw config text before JSON parsing, with
# no escape syntax: ``}`` ends the token, and the JSON escapes for ``"``, ``\`` and
# control characters are looked up as literal path text. A path containing any of them
# cannot be named, and OpenCode rejects the whole config (HTTP 500) when it tries. Lone
# surrogates are the undecodable bytes of a non-UTF-8 path, which OpenCode reads as text.
_UNNAMEABLE_PATH_CHARS = re.compile(r'[}"\\\x00-\x1f\ud800-\udfff]')


def dump_opencode_config_content(config: Mapping[str, object]) -> str:
    """Serialize ``OPENCODE_CONFIG_CONTENT``; every writer of it must use this.

    OpenCode expands ``{file:<path>}`` references on the raw text before parsing it, so
    a non-ASCII path must stay raw: a JSON ``\\u`` escape is looked up as literal path text.
    """

    return json.dumps(config, separators=(",", ":"), ensure_ascii=False)


def opencode_model_parts(model: str) -> tuple[str, str]:
    provider, separator, model_id = model.partition("/")
    if not separator or not provider.strip() or not model_id.strip():
        raise HarnessCapabilityMismatch(
            "OpenCode requires a provider-qualified model: provider/model"
        )
    return provider, model_id


def meridian_agent_id(profile: str | None) -> str:
    """Name the default agent after the profile.

    The prefix avoids ordinary user-agent collisions.
    """

    slug = _UNSAFE_AGENT_ID_CHARS.sub("-", (profile or "").strip()).strip("-")
    return f"{_AGENT_ID_PREFIX}-{slug}" if slug else _AGENT_ID_PREFIX


def _file_reference(prompt_file: Path) -> str:
    path = prompt_file.as_posix()
    unsafe = sorted(set(_UNNAMEABLE_PATH_CHARS.findall(path)))
    if unsafe:
        raise HarnessCapabilityMismatch(
            f"OpenCode cannot load the system prompt from {path!r}: its config "
            f"{{file:}} reference cannot express {', '.join(repr(c) for c in unsafe)}. "
            "Use a runtime directory whose path avoids these characters."
        )
    return f"{{file:{path}}}"


def _with_root_grants(
    permissions: OpenCodePermissions, roots: tuple[Path, ...]
) -> OpenCodePermissions:
    return (
        *permissions,
        *(
            OpenCodePermissionRule(
                action="external_directory",
                resource=root.as_posix() + "/**",
                effect="allow",
            )
            for root in roots
        ),
    )


def _project_v1_permissions(permissions: OpenCodePermissions) -> dict[str, object]:
    """Encode ordered rules as V1's nested permission map."""

    grouped: dict[str, list[OpenCodePermissionRule]] = {}
    for rule in permissions:
        grouped.setdefault(rule.action, []).append(rule)
    projected: dict[str, object] = {}
    for action, rules in grouped.items():
        if len(rules) == 1 and rules[0].resource == "*":
            projected[action] = rules[0].effect
        else:
            projected[action] = {rule.resource: rule.effect for rule in rules}
    return projected


def _project_v2_permissions(permissions: OpenCodePermissions) -> list[dict[str, str]]:
    """Serialize the compile's already-canonical ordered V2 rules."""

    return [
        {"action": rule.action, "resource": rule.resource, "effect": rule.effect}
        for rule in permissions
    ]


def project_opencode_launch_config(
    raw: str | None,
    *,
    version: OpenCodeVersion,
    roots: tuple[Path, ...],
    agent_id: str,
    prompt_file: Path | None,
    permissions: OpenCodePermissions,
    model: str | None,
) -> str:
    """Return config carrying Meridian's reserved agent and unrelated inherited settings.

    Workspace grants follow the compiled policy so a broad deny cannot shadow them.
    ``prompt_file`` is optional, but when present it must exist before OpenCode starts: a
    missing ``{file:}`` target fails the whole config. The reserved agent entry is replaced;
    unrelated user agents and every other key in ``raw`` survive.
    """

    parsed: object = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = dict(cast("dict[str, object]", parsed))
    if model is not None:
        opencode_model_parts(model)
        config["model"] = model
    native_permissions = _with_root_grants(permissions, roots)
    agents_key = "agent" if version == "v1" else "agents"
    agents = config.get(agents_key, {})
    if not isinstance(agents, dict):
        raise HarnessCapabilityMismatch("OpenCode agent configuration must be a JSON object")
    agent: dict[str, object] = {
        "mode": "primary",
        ("permission" if version == "v1" else "permissions"): (
            _project_v1_permissions(native_permissions)
            if version == "v1"
            else _project_v2_permissions(native_permissions)
        ),
    }
    if prompt_file is not None:
        agent["prompt" if version == "v1" else "system"] = _file_reference(prompt_file)
    config[agents_key] = {**cast("dict[str, object]", agents), agent_id: agent}
    config["default_agent"] = agent_id
    return dump_opencode_config_content(config)


__all__ = [
    "OPENCODE_CONFIG_CONTENT_ENV",
    "dump_opencode_config_content",
    "meridian_agent_id",
    "opencode_model_parts",
    "project_opencode_launch_config",
]
