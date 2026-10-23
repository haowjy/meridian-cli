"""Harness-native config projection and enforcement-report helpers."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import cast

from meridian.lib.core.execution_policy import NativeConfig
from meridian.lib.safety.policy import EnforcementReport, ReportLine

CODEX_CONFIG_MODEL_KEY = "model"
CODEX_CONFIG_REASONING_EFFORT_KEY = "model_reasoning_effort"
CODEX_CONFIG_AUTO_COMPACT_TOKEN_LIMIT_KEY = "model_auto_compact_token_limit"
CODEX_CONFIG_POST_TURN_COMPACT_THRESHOLD_PERCENT_KEY = (
    "model_post_turn_compact_threshold_percent"
)
CODEX_CONFIG_SANDBOX_MODE_KEY = "sandbox_mode"
CODEX_CONFIG_APPROVAL_POLICY_KEY = "approval_policy"
CODEX_CONFIG_WRITABLE_ROOTS_KEY = "sandbox_workspace_write.writable_roots"
CODEX_CONFIG_NETWORK_ACCESS_KEY = "sandbox_workspace_write.network_access"
CODEX_CONFIG_NOTIFY_KEY = "notify"
CODEX_CONFIG_WEB_SEARCH_KEY = "web_search"
CODEX_CONFIG_TOOLS_WEB_SEARCH_KEY = "tools.web_search"
CODEX_CONFIG_FEATURES_PREFIX = "features"
CODEX_CONFIG_AGENTS_PREFIX = "agents"

# These values are projected by Meridian's Codex process flags, app-server flags, or
# thread request. Native passthrough may add siblings, but never replace these owners.
CODEX_RESERVED_NATIVE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        CODEX_CONFIG_MODEL_KEY,
        CODEX_CONFIG_REASONING_EFFORT_KEY,
        CODEX_CONFIG_AUTO_COMPACT_TOKEN_LIMIT_KEY,
        CODEX_CONFIG_POST_TURN_COMPACT_THRESHOLD_PERCENT_KEY,
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
)


type NativeConfigLeaf = tuple[tuple[str, ...], object]

_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def flatten_native_config(config: NativeConfig) -> tuple[NativeConfigLeaf, ...]:
    """Flatten nested mappings while retaining their original key segments."""

    flattened: list[NativeConfigLeaf] = []

    def visit(mapping: Mapping[str, object], prefix: tuple[str, ...] = ()) -> None:
        for key, value in mapping.items():
            segments = (*prefix, key)
            if isinstance(value, Mapping):
                nested = cast("Mapping[str, object]", value)
                if nested:
                    visit(nested, segments)
                    continue
                flattened.append((segments, nested))
                continue
            flattened.append((segments, value))

    visit(config)
    return tuple(flattened)


def _toml_basic_string(value: str) -> str:
    # JSON's double-quoted escaping is also TOML basic-string escaping for the
    # control characters accepted here. Keep Unicode literal so astral characters
    # do not become JSON surrogate pairs, which TOML rejects. JSON leaves DEL literal;
    # TOML basic strings forbid it, and Codex would silently reparse the argument as a
    # plain string.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_inline_key(value: object) -> str:
    key = str(value)
    return key if _TOML_BARE_KEY.fullmatch(key) else _toml_basic_string(key)


def codex_toml_literal(value: object) -> str | None:
    """Render a native Codex value as TOML, or return ``None`` if it cannot be encoded.

    Mars emits JSON-compatible values. The defensive ``None`` result also keeps
    older or hand-written snapshots from turning an unsupported leaf into a launch
    traceback.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        # TOML integers are 64-bit signed; Codex reparses anything larger as a string.
        return str(value) if -(2**63) <= value < 2**63 else None
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _toml_basic_string(value)
    if isinstance(value, Mapping):
        entries: list[str] = []
        for key, item in cast("Mapping[object, object]", value).items():
            literal = codex_toml_literal(item)
            if literal is None:
                return None
            entries.append(f"{_toml_inline_key(key)} = {literal}")
        return f"{{ {', '.join(entries)} }}" if entries else "{}"
    if isinstance(value, tuple | list):
        items: list[str] = []
        for item in cast("Sequence[object]", value):
            literal = codex_toml_literal(item)
            if literal is None:
                return None
            items.append(literal)
        return f"[{', '.join(items)}]"
    return None


def _codex_key_addressable(segments: tuple[str, ...]) -> bool:
    return all(
        segment
        and not any(character in segment for character in ".='\"")
        and not any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in segment
        )
        for segment in segments
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
    for segments, value in flatten_native_config(config):
        key = ".".join(segments)
        if not _codex_key_addressable(segments):
            lines.append(
                ReportLine(
                    f"native_config: {key}",
                    "unsupported",
                    "Codex -c cannot address this key",
                )
            )
            continue
        if isinstance(value, Mapping):
            mapping_value = cast("Mapping[str, object]", value)
            if not mapping_value:
                lines.append(
                    ReportLine(
                        f"native_config: {key} = {{}}",
                        "unsupported",
                        "empty mappings have no Codex -c leaf to override",
                    )
                )
                continue
        literal = codex_toml_literal(cast("object", value))
        if literal is None:
            lines.append(
                ReportLine(
                    f"native_config: {key}",
                    "unsupported",
                    "Codex -c cannot encode this value as TOML",
                )
            )
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

    keys = tuple(".".join(segments) for segments, _value in flatten_native_config(config))
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
    "CODEX_CONFIG_AUTO_COMPACT_TOKEN_LIMIT_KEY",
    "CODEX_CONFIG_FEATURES_PREFIX",
    "CODEX_CONFIG_MODEL_KEY",
    "CODEX_CONFIG_NETWORK_ACCESS_KEY",
    "CODEX_CONFIG_NOTIFY_KEY",
    "CODEX_CONFIG_POST_TURN_COMPACT_THRESHOLD_PERCENT_KEY",
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
