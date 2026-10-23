"""Shared Codex projection utilities used by multiple launch transports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_AGENTS_PREFIX,
    CODEX_CONFIG_FEATURES_PREFIX,
    CODEX_CONFIG_NETWORK_ACCESS_KEY,
    CODEX_CONFIG_SANDBOX_MODE_KEY,
    CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY,
    CODEX_CONFIG_WEB_SEARCH_KEY,
    CODEX_CONFIG_WRITABLE_ROOTS_KEY,
)
from meridian.lib.harness.projections.permissions_codex import CodexPermissions

type CodexLaunchTransport = Literal["subprocess", "streaming"]

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


@dataclass(frozen=True)
class CodexSandboxProjection:
    """One sandbox configuration, spelled for each Codex launch boundary."""

    sandbox_mode: str | None
    writable_roots: tuple[str, ...]
    network_access: bool | None = None

    def process_args(self, transport: CodexLaunchTransport) -> tuple[str, ...]:
        """Return sandbox arguments for ``codex exec`` or ``codex app-server``."""

        arguments: list[str] = []
        if self.sandbox_mode is not None:
            if transport == "subprocess":
                arguments.extend(("--sandbox", self.sandbox_mode))
            else:
                arguments.extend(
                    (
                        "-c",
                        f"{CODEX_CONFIG_SANDBOX_MODE_KEY}={json.dumps(self.sandbox_mode)}",
                    )
                )

        if transport == "subprocess":
            for root in self.writable_roots:
                arguments.extend(("--add-dir", root))
        elif self.writable_roots:
            arguments.extend(
                (
                    "-c",
                    f"{CODEX_CONFIG_WRITABLE_ROOTS_KEY}="
                    f"{json.dumps(list(self.writable_roots))}",
                )
            )

        if self.network_access is not None:
            arguments.extend(
                (
                    "-c",
                    f"{CODEX_CONFIG_NETWORK_ACCESS_KEY}="
                    f"{str(self.network_access).lower()}",
                )
            )
        return tuple(arguments)

    def thread_fields(self) -> tuple[dict[str, object], dict[str, object]]:
        """Return thread request fields and config overrides for app-server."""

        fields: dict[str, object] = {}
        if self.sandbox_mode is not None:
            fields["sandbox"] = self.sandbox_mode

        config: dict[str, object] = {}
        if self.writable_roots:
            config[CODEX_CONFIG_WRITABLE_ROOTS_KEY] = list(self.writable_roots)
        if self.network_access is not None:
            config[CODEX_CONFIG_NETWORK_ACCESS_KEY] = self.network_access
        return fields, config


def project_codex_sandbox(
    permissions: CodexPermissions,
    *,
    writable_roots: tuple[str, ...],
    network_access: bool | None = None,
) -> CodexSandboxProjection:
    """Build the shared sandbox projection consumed by both Codex transports."""

    return CodexSandboxProjection(
        sandbox_mode=permissions.sandbox_mode,
        writable_roots=writable_roots,
        network_access=network_access,
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
    "CodexLaunchTransport",
    "CodexSandboxProjection",
    "project_codex_sandbox",
    "project_codex_tool_switch_flags",
]
