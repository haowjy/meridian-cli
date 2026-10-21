"""OpenCode launch config projected at bind: Meridian's default agent and model.

OpenCode has no per-request system field that both major versions honor, so the
composed prompt rides ``OPENCODE_CONFIG_CONTENT`` as a primary agent whose prompt
is ``{file:<system-prompt.md>}`` and which ``default_agent`` selects. Every launch
path (streaming spawn, managed primary, bare-TUI fallback) inherits the bound
child env, so none needs its own delivery channel.

V1's ``agent.<id>.prompt`` shape is the single shape for both versions: V2
normalizes it to ``agents.<id>.system``. The agent carries no ``model`` because
V2 ignores it; the model is the top-level config ``model``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from meridian.lib.harness.projections.project_opencode_streaming import opencode_model_parts
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch

_AGENT_ID_PREFIX = "meridian"
_UNSAFE_AGENT_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def meridian_agent_id(profile: str | None) -> str:
    """Name the default agent after the profile, prefixed so it never merges into a user agent."""

    slug = _UNSAFE_AGENT_ID_CHARS.sub("-", (profile or "").strip()).strip("-")
    return f"{_AGENT_ID_PREFIX}-{slug}" if slug else _AGENT_ID_PREFIX


def project_opencode_launch_config(
    raw: str | None,
    *,
    agent_id: str,
    prompt_file: Path | None,
    model: str | None,
) -> str | None:
    """Return config content carrying the default agent and model, or None if neither applies.

    Every other key in ``raw`` survives. ``prompt_file`` must exist before OpenCode
    starts: a missing ``{file:}`` target fails the whole config.
    """

    if prompt_file is None and model is None:
        return None
    parsed: object = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = dict(cast("dict[str, object]", parsed))
    if model is not None:
        opencode_model_parts(model)
        config["model"] = model
    if prompt_file is not None:
        agents = config.get("agent", {})
        if not isinstance(agents, dict):
            raise HarnessCapabilityMismatch("OpenCode agent configuration must be a JSON object")
        config["agent"] = {
            **cast("dict[str, object]", agents),
            agent_id: {"prompt": f"{{file:{prompt_file.as_posix()}}}", "mode": "primary"},
        }
        config["default_agent"] = agent_id
    return json.dumps(config, separators=(",", ":"))


__all__ = ["meridian_agent_id", "project_opencode_launch_config"]
