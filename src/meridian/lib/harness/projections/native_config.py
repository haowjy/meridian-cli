"""Harness-native config projection and enforcement-report helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from meridian.lib.core.execution_policy import NativeConfig
from meridian.lib.safety.policy import EnforcementReport, ReportLine

CODEX_CONFIG_MODEL_KEY = "model"
CODEX_CONFIG_REASONING_EFFORT_KEY = "model_reasoning_effort"
CODEX_CONFIG_SANDBOX_MODE_KEY = "sandbox_mode"
CODEX_CONFIG_APPROVAL_POLICY_KEY = "approval_policy"
CODEX_CONFIG_WRITABLE_ROOTS_KEY = "sandbox_workspace_write.writable_roots"
CODEX_CONFIG_NOTIFY_KEY = "notify"
CODEX_CONFIG_WEB_SEARCH_KEY = "web_search"
CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY = "tools.web_search"
CODEX_CONFIG_FEATURES_PREFIX = "features"
CODEX_CONFIG_AGENTS_PREFIX = "agents"
CODEX_CONFIG_MCP_SERVERS_PREFIX = "mcp.servers"

# These values are projected by Meridian's Codex process flags, app-server flags, or
# thread request. Native passthrough may add siblings, but never replace these owners.
CODEX_RESERVED_NATIVE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        CODEX_CONFIG_MODEL_KEY,
        CODEX_CONFIG_REASONING_EFFORT_KEY,
        CODEX_CONFIG_SANDBOX_MODE_KEY,
        CODEX_CONFIG_APPROVAL_POLICY_KEY,
        CODEX_CONFIG_WRITABLE_ROOTS_KEY,
        CODEX_CONFIG_NOTIFY_KEY,
        CODEX_CONFIG_WEB_SEARCH_KEY,
        CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY,
    }
)
CODEX_RESERVED_NATIVE_CONFIG_PREFIXES: tuple[str, ...] = (
    CODEX_CONFIG_FEATURES_PREFIX,
    CODEX_CONFIG_AGENTS_PREFIX,
    CODEX_CONFIG_MCP_SERVERS_PREFIX,
)


def flatten_native_config(config: NativeConfig) -> tuple[tuple[str, object], ...]:
    """Flatten nested native mappings to ordered dotted-key leaves."""

    flattened: list[tuple[str, object]] = []

    def visit(mapping: Mapping[str, object], prefix: str = "") -> None:
        for key, value in mapping.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, Mapping):
                visit(cast("Mapping[str, object]", value), dotted)
            else:
                flattened.append((dotted, value))

    visit(config)
    return tuple(flattened)


def _toml_basic_string(value: str) -> str:
    # JSON's double-quoted escaping is also TOML basic-string escaping for the
    # control characters accepted here. Keep Unicode literal so astral characters
    # do not become JSON surrogate pairs, which TOML rejects.
    return json.dumps(value, ensure_ascii=False)


def codex_toml_literal(value: object) -> str | None:
    """Render a native Codex value as the supported TOML literal subset.

    Null has no Codex override and is omitted. Mars normally removes nulls before
    producing a launch bundle; accepting one here keeps older or hand-written
    snapshots truncation-safe.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_basic_string(value)
    if isinstance(value, tuple | list):
        items: list[str] = []
        for item in cast("Sequence[object]", value):
            literal = codex_toml_literal(item)
            if literal is None:
                continue
            if isinstance(item, tuple | list | Mapping):
                raise ValueError("Codex native_config arrays may contain only scalars")
            items.append(literal)
        return f"[{', '.join(items)}]"
    raise ValueError(
        "Codex native_config values must be booleans, integers, strings, "
        f"or arrays of those; got {value!r}"
    )


def _reserved_codex_key(key: str) -> bool:
    if any(
        key == reserved or key.startswith(f"{reserved}.")
        for reserved in CODEX_RESERVED_NATIVE_CONFIG_KEYS
    ):
        return True
    return any(
        key == prefix or key.startswith(f"{prefix}.")
        for prefix in CODEX_RESERVED_NATIVE_CONFIG_PREFIXES
    )


def project_codex_native_config(
    config: NativeConfig,
) -> tuple[tuple[str, ...], EnforcementReport]:
    """Build Codex ``-c`` pairs and report lines from one native config tree."""

    arguments: list[str] = []
    lines: list[ReportLine] = []
    for key, value in flatten_native_config(config):
        literal = codex_toml_literal(value)
        if literal is None:
            continue
        element = f"native_config: {key} = {literal}"
        if _reserved_codex_key(key):
            lines.append(
                ReportLine(
                    element,
                    "unsupported",
                    "skipped because Meridian owns this Codex config key",
                )
            )
            continue
        override = f"{key}={literal}"
        arguments.extend(("-c", override))
        lines.append(ReportLine(element, "native", f"-c {override}"))
    return tuple(arguments), tuple(lines)


def unsupported_native_config_report(
    config: NativeConfig, *, harness: str
) -> EnforcementReport:
    """Report a non-empty native config that a harness does not project."""

    keys = tuple(key for key, _value in flatten_native_config(config))
    if not keys:
        return ()
    return (
        ReportLine(
            f"native_config: {', '.join(keys)}",
            "unsupported",
            f"not applied for {harness}",
        ),
    )


__all__ = [
    "CODEX_CONFIG_AGENTS_PREFIX",
    "CODEX_CONFIG_APPROVAL_POLICY_KEY",
    "CODEX_CONFIG_FEATURES_PREFIX",
    "CODEX_CONFIG_MCP_SERVERS_PREFIX",
    "CODEX_CONFIG_MODEL_KEY",
    "CODEX_CONFIG_NOTIFY_KEY",
    "CODEX_CONFIG_REASONING_EFFORT_KEY",
    "CODEX_CONFIG_SANDBOX_MODE_KEY",
    "CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY",
    "CODEX_CONFIG_WEB_SEARCH_KEY",
    "CODEX_CONFIG_WRITABLE_ROOTS_KEY",
    "CODEX_RESERVED_NATIVE_CONFIG_KEYS",
    "CODEX_RESERVED_NATIVE_CONFIG_PREFIXES",
    "codex_toml_literal",
    "flatten_native_config",
    "project_codex_native_config",
    "unsupported_native_config_report",
]
