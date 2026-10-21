"""OpenCode child config projected at bind: workspace grants, default agent, model.

The one producer of ``OPENCODE_CONFIG_CONTENT`` at bind; the V2 connection's permission
merge re-serializes it and must use ``dump_opencode_config_content``. The composed prompt
rides this config as a primary agent whose prompt is ``{file:<system-prompt.md>}``;
``default_agent`` selects it only for sessions with no committed agent, so each surface
that opens or resumes a session selects ``ResolvedLaunchSpec.agent_name`` itself. The agent
carries no ``model``: V2 ignores it, so the model is the top-level config ``model``.

Why this channel, and what each surface selects: ``harness/.context/CONTEXT.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

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
    """Name the default agent after the profile, prefixed so it never merges into a user agent."""

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


def _permission_rules(raw: object) -> dict[str, object]:
    """Expand native scalar actions without losing last-match rule ordering."""
    if isinstance(raw, str) and raw in {"allow", "deny", "ask"}:
        return {"*": raw}
    if isinstance(raw, dict):
        return dict(cast("dict[str, object]", raw))
    raise HarnessCapabilityMismatch(
        "OpenCode permissions must be an action or an ordered rule object"
    )


def _grant_external_directories(config: dict[str, object], roots: tuple[Path, ...]) -> None:
    permission = _permission_rules(config.get("permission", {}))
    external_directory = _permission_rules(permission.get("external_directory", {}))
    for root in roots:
        pattern = root.as_posix() + "/**"
        external_directory.pop(pattern, None)
        external_directory[pattern] = "allow"
    permission["external_directory"] = external_directory
    config["permission"] = permission


def project_opencode_launch_config(
    raw: str | None,
    *,
    roots: tuple[Path, ...],
    agent_id: str | None,
    prompt_file: Path,
    model: str | None,
) -> str | None:
    """Return config content carrying roots, default agent and model, or None if none applies.

    ``roots`` get ``external_directory`` grants. The agent and its ``prompt_file`` prompt
    are emitted exactly when ``agent_id`` is set. Every other key in ``raw`` survives.
    ``prompt_file`` must exist before OpenCode starts: a missing ``{file:}`` target fails
    the whole config.
    """

    if not roots and agent_id is None and model is None:
        return None
    parsed: object = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = dict(cast("dict[str, object]", parsed))
    if roots:
        _grant_external_directories(config, roots)
    if model is not None:
        opencode_model_parts(model)
        config["model"] = model
    if agent_id is not None:
        agents = config.get("agent", {})
        if not isinstance(agents, dict):
            raise HarnessCapabilityMismatch("OpenCode agent configuration must be a JSON object")
        config["agent"] = {
            **cast("dict[str, object]", agents),
            agent_id: {"prompt": _file_reference(prompt_file), "mode": "primary"},
        }
        config["default_agent"] = agent_id
    return dump_opencode_config_content(config)


__all__ = [
    "OPENCODE_CONFIG_CONTENT_ENV",
    "dump_opencode_config_content",
    "meridian_agent_id",
    "opencode_model_parts",
    "project_opencode_launch_config",
]
