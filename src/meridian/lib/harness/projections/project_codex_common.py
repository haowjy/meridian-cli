"""Shared Codex projection utilities used by multiple launch transports."""

from __future__ import annotations

from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_AGENTS_PREFIX,
    CODEX_CONFIG_FEATURES_PREFIX,
    CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY,
    CODEX_CONFIG_WEB_SEARCH_KEY,
)
from meridian.lib.harness.projections.permissions_codex import CodexPermissions

# Codex 0.162: disabling v2 falls back to v1, whose tools survive `--disable multi_agent`;
# only a zero spawn depth removes them. Config overrides, not `--disable`: Codex rejects
# `--disable` for a feature it does not know (older builds), but ignores an unknown
# `features.<name>=false`.
CODEX_DISABLE_BUILTIN_AGENT_FLAGS: tuple[str, ...] = (
    "-c",
    f"{CODEX_CONFIG_FEATURES_PREFIX}.multi_agent_v2=false",
    "-c",
    f"{CODEX_CONFIG_AGENTS_PREFIX}.max_depth=0",
)


def project_codex_tool_switch_flags(permissions: CodexPermissions) -> tuple[str, ...]:
    """Spell Codex's process-level tool switches for either launch transport."""

    projected: list[str] = []
    if permissions.shell_tool is not None:
        projected.extend(
            (
                "-c",
                f"{CODEX_CONFIG_FEATURES_PREFIX}.shell_tool="
                f"{str(permissions.shell_tool).lower()}",
            )
        )
    if permissions.web_search is True:
        projected.extend(("-c", f"{CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY}=true"))
    elif permissions.web_search is False:
        projected.extend(("-c", f'{CODEX_CONFIG_WEB_SEARCH_KEY}="disabled"'))
    if permissions.view_image is not None:
        projected.extend(("--enable" if permissions.view_image else "--disable", "view_image"))
    return tuple(projected)


__all__ = [
    "CODEX_DISABLE_BUILTIN_AGENT_FLAGS",
    "project_codex_tool_switch_flags",
]
