"""Shared Codex projection utilities used by multiple launch transports."""

from __future__ import annotations

import json
from collections.abc import Iterable

from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch

_APPROVAL_POLICY_BY_MODE: dict[str, str | None] = {
    "default": None,
    "auto": "on-request",
    "confirm": "untrusted",
    "never": "never",
    "yolo": "never",
}

_SANDBOX_MODE_BY_MODE: dict[str, str | None] = {
    "default": None,
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "danger-full-access": "danger-full-access",
}


def map_codex_approval_policy(approval_mode: str) -> str | None:
    """Map Meridian approval mode to Codex approval policy."""

    if approval_mode not in _APPROVAL_POLICY_BY_MODE:
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested approval mode "
            f"'{approval_mode}' on this CLI/protocol version"
        )
    mapped = _APPROVAL_POLICY_BY_MODE[approval_mode]
    if mapped is None and approval_mode != "default":
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested approval mode "
            f"'{approval_mode}' on this CLI/protocol version"
        )
    return mapped


def map_codex_sandbox_mode(sandbox_mode: str) -> str | None:
    """Map Meridian sandbox mode to Codex sandbox mode."""

    if sandbox_mode not in _SANDBOX_MODE_BY_MODE:
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested sandbox mode "
            f"'{sandbox_mode}' on this CLI/protocol version"
        )
    mapped = _SANDBOX_MODE_BY_MODE[sandbox_mode]
    if mapped is None and sandbox_mode != "default":
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested sandbox mode "
            f"'{sandbox_mode}' on this CLI/protocol version"
        )
    return mapped


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


__all__ = [
    "HarnessCapabilityMismatch",
    "map_codex_approval_policy",
    "map_codex_sandbox_mode",
    "project_codex_mcp_config_flags",
]
