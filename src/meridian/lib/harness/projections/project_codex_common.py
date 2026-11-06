"""Shared Codex projection utilities used by multiple launch transports."""

from __future__ import annotations

import json
from dataclasses import dataclass

from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_AGENTS_PREFIX,
    CODEX_CONFIG_FEATURES_PREFIX,
    CODEX_CONFIG_NETWORK_ACCESS_KEY,
    CODEX_CONFIG_SANDBOX_MODE_KEY,
    CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY,
    CODEX_CONFIG_WEB_SEARCH_KEY,
    CODEX_CONFIG_WRITABLE_ROOTS_KEY,
    codex_toml_literal,
)
from meridian.lib.harness.projections.permissions_codex import CodexPermissions
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

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
_CODEX_NETWORK_PROXY_ENABLED_KEY = "features.network_proxy.enabled"
_CODEX_NETWORK_PROXY_DOMAINS_KEY = "features.network_proxy.domains"


@dataclass(frozen=True)
class CodexSandboxProjection:
    """One sandbox configuration, spelled for each Codex launch boundary."""

    sandbox_mode: str | None
    writable_roots: tuple[str, ...]
    network_access: bool | None
    network_proxy_enabled: bool
    network_domains: tuple[tuple[str, str], ...]

    def subprocess_args(self) -> tuple[str, ...]:
        """Return sandbox arguments for ``codex exec``."""

        arguments: list[str] = []
        if self.sandbox_mode is not None:
            arguments.extend(("--sandbox", self.sandbox_mode))

        for root in self.writable_roots:
            arguments.extend(("--add-dir", root))

        self._append_network_args(arguments)
        return tuple(arguments)

    def appserver_args(self) -> tuple[str, ...]:
        """Return sandbox arguments for ``codex app-server``."""

        arguments: list[str] = []
        if self.sandbox_mode is not None:
            arguments.extend(
                (
                    "-c",
                    f"{CODEX_CONFIG_SANDBOX_MODE_KEY}={json.dumps(self.sandbox_mode)}",
                )
            )

        if self.writable_roots:
            arguments.extend(
                (
                    "-c",
                    f"{CODEX_CONFIG_WRITABLE_ROOTS_KEY}={json.dumps(list(self.writable_roots))}",
                )
            )

        self._append_network_args(arguments)
        return tuple(arguments)

    def _append_network_args(self, arguments: list[str]) -> None:
        if self.network_access is not None:
            arguments.extend(
                (
                    "-c",
                    f"{CODEX_CONFIG_NETWORK_ACCESS_KEY}={str(self.network_access).lower()}",
                )
            )
        if self.network_proxy_enabled:
            arguments.extend(("-c", f"{_CODEX_NETWORK_PROXY_ENABLED_KEY}=true"))
            domains = codex_toml_literal(dict(self.network_domains))
            assert domains is not None
            arguments.extend(("-c", f"{_CODEX_NETWORK_PROXY_DOMAINS_KEY}={domains}"))

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
        if self.network_proxy_enabled:
            config[_CODEX_NETWORK_PROXY_ENABLED_KEY] = True
            config[_CODEX_NETWORK_PROXY_DOMAINS_KEY] = dict(self.network_domains)
        return fields, config


def project_codex_sandbox(
    spec: ResolvedLaunchSpec,
    permissions: CodexPermissions,
) -> CodexSandboxProjection:
    """Build the shared sandbox projection consumed by both Codex transports."""

    return CodexSandboxProjection(
        sandbox_mode=permissions.sandbox_mode,
        writable_roots=tuple(root.as_posix() for root in spec.projected_roots),
        network_access=permissions.network.network_access,
        network_proxy_enabled=permissions.network.proxy_enabled,
        network_domains=permissions.network.domains,
    )


def project_codex_tool_switch_flags(permissions: CodexPermissions) -> tuple[str, ...]:
    """Spell Codex's process-level tool switches for either launch transport."""

    projected: list[str] = []
    if permissions.shell_tool is not None:
        projected.extend(
            (
                "-c",
                f"{CODEX_CONFIG_FEATURES_PREFIX}.shell_tool={str(permissions.shell_tool).lower()}",
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
    "CodexSandboxProjection",
    "project_codex_sandbox",
    "project_codex_tool_switch_flags",
]
