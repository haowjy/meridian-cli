"""OpenCode launch config projected at bind: Meridian's default agent and model.

OpenCode has no per-request system field that both major versions honor, so the
composed prompt rides ``OPENCODE_CONFIG_CONTENT`` as a primary agent whose prompt
is ``{file:<system-prompt.md>}`` and which ``default_agent`` selects. Every launch
path (streaming spawn, managed primary, bare-TUI fallback) inherits the bound
child env, so none needs its own delivery channel.

V1's ``agent.<id>.prompt`` shape is the single shape for both versions: V2
normalizes it to ``agents.<id>.system``. The agent carries no ``model`` because
V2 ignores it; the model is the top-level config ``model``.

``default_agent`` only covers sessions that have no committed agent. A resumed V1
session replays its last message's agent and a V2 session keeps its stored agent, so
each surface that opens or resumes a session also selects the agent explicitly
(``ResolvedLaunchSpec.agent_name``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from meridian.lib.harness.projections.project_opencode_streaming import opencode_model_parts
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.workspace_projection import dump_opencode_config_content

_AGENT_ID_PREFIX = "meridian"
_UNSAFE_AGENT_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
# OpenCode substitutes ``{file:<path>}`` on the raw config text before JSON parsing, with
# no escape syntax: ``}`` ends the token, and the JSON escapes for ``"``, ``\`` and
# control characters are looked up as literal path text. A path containing any of them
# cannot be named, and OpenCode rejects the whole config (HTTP 500) when it tries. Lone
# surrogates are the undecodable bytes of a non-UTF-8 path, which OpenCode reads as text.
_UNNAMEABLE_PATH_CHARS = re.compile(r'[}"\\\x00-\x1f\ud800-\udfff]')


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


def project_opencode_launch_config(
    raw: str | None,
    *,
    agent_id: str | None,
    prompt_file: Path,
    model: str | None,
) -> str | None:
    """Return config content carrying the default agent and model, or None if neither applies.

    The agent and its ``prompt_file`` prompt are emitted exactly when ``agent_id`` is
    set. Every other key in ``raw`` survives. ``prompt_file`` must exist before OpenCode
    starts: a missing ``{file:}`` target fails the whole config.
    """

    if agent_id is None and model is None:
        return None
    parsed: object = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = dict(cast("dict[str, object]", parsed))
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


__all__ = ["meridian_agent_id", "project_opencode_launch_config"]
