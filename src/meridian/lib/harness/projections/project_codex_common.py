"""Shared Codex projection utilities used by multiple launch transports."""

from __future__ import annotations

import json
from collections.abc import Iterable

from meridian.lib.harness.projections.permissions_codex import CodexPermissions

# Codex 0.162: disabling v2 falls back to v1, whose tools survive `--disable multi_agent`;
# only a zero spawn depth removes them. Config overrides, not `--disable`: Codex rejects
# `--disable` for a feature it does not know (older builds), but ignores an unknown
# `features.<name>=false`.
CODEX_DISABLE_BUILTIN_AGENT_FLAGS: tuple[str, ...] = (
    "-c",
    "features.multi_agent_v2=false",
    "-c",
    "agents.max_depth=0",
)


def project_codex_mcp_config_flags(mcp_tools: Iterable[str]) -> tuple[str, ...]:
    """Project ``mcp_tools`` to Codex ``-c mcp.servers.*.command=...`` flags."""

    projected: list[str] = []
    for raw_entry in mcp_tools:
        entry = raw_entry.strip()
        if not entry:
            continue
        name, separator, command = entry.partition("=")
        if not separator or not name.strip() or not command.strip():
            raise ValueError(
                f"Codex mcp_tools entries must be '<name>=<command>'; got {raw_entry!r}"
            )
        projected.extend(
            (
                "-c",
                f"mcp.servers.{name.strip()}.command={json.dumps(command.strip())}",
            )
        )
    return tuple(projected)


def project_codex_tool_switch_flags(permissions: CodexPermissions) -> tuple[str, ...]:
    """Spell Codex's process-level tool switches for either launch transport."""

    projected: list[str] = []
    if permissions.shell_tool is not None:
        projected.extend(("-c", f"features.shell_tool={str(permissions.shell_tool).lower()}"))
    if permissions.web_search is True:
        projected.extend(("-c", "tools.web_search=true"))
    elif permissions.web_search is False:
        projected.extend(("-c", 'web_search="disabled"'))
    if permissions.view_image is not None:
        projected.extend(("--enable" if permissions.view_image else "--disable", "view_image"))
    return tuple(projected)


__all__ = [
    "CODEX_DISABLE_BUILTIN_AGENT_FLAGS",
    "project_codex_mcp_config_flags",
    "project_codex_tool_switch_flags",
]
