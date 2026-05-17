"""Shared Codex projection utilities used by multiple launch transports."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch

_APPROVAL_POLICY_BY_MODE: dict[str, str | None] = {
    "default": None,
    "auto": "on-request",
    "confirm": "untrusted",
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


def _serialize_codex_native_config_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        list_value = list(cast("list[Any] | tuple[Any, ...]", value))
        return json.dumps(list_value, separators=(",", ":"))
    return None


def project_codex_native_config_flags(
    native_config: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Project Mars bundle native_config to deterministic Codex ``-c`` args.

    Returns ``(flags, warnings)`` where warnings contains user-facing messages
    for skipped values that cannot be represented in this transport.
    """

    projected: list[str] = []
    warnings: list[str] = []
    for key in sorted(native_config):
        value = native_config[key]
        if isinstance(value, dict):
            warnings.append(
                "native-config key "
                f"'{key}' has nested map value; Codex -c requires scalar/array values "
                "or dotted keys. Skipped."
            )
            continue
        serialized = _serialize_codex_native_config_value(value)
        if serialized is None:
            warnings.append(
                "native-config key "
                f"'{key}' has unsupported value type '{type(value).__name__}' "
                "for Codex -c. Skipped."
            )
            continue
        projected.extend(("-c", f"{key}={serialized}"))
    return tuple(projected), tuple(warnings)


__all__ = [
    "HarnessCapabilityMismatch",
    "map_codex_approval_policy",
    "map_codex_sandbox_mode",
    "project_codex_mcp_config_flags",
    "project_codex_native_config_flags",
]
