"""Child-process environment helpers shared by launch and spawn paths."""

from collections.abc import Callable, Collection, Mapping
from typing import cast

from meridian.lib.core.child_env import ALLOWED_CHILD_ENV_KEYS
from meridian.lib.core.overrides import RUNTIME_OVERRIDE_ENV_VARS
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.connections.base import PiSessionRole
from meridian.lib.safety.permissions import PermissionConfig

from .constants import BLOCKED_CHILD_ENV_VARS

_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)
_CHILD_ENV_ALLOWLIST_PREFIXES = ("LC_", "XDG_", "UV_")
_CHILD_ENV_SECRET_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET")


def _is_allowlisted_child_env_var(key: str) -> bool:
    normalized = key.upper()
    if normalized in _CHILD_ENV_ALLOWLIST:
        return True
    return any(normalized.startswith(prefix) for prefix in _CHILD_ENV_ALLOWLIST_PREFIXES)


def _looks_like_secret_env_var(key: str) -> bool:
    normalized = key.upper()
    return any(normalized.endswith(suffix) for suffix in _CHILD_ENV_SECRET_SUFFIXES)


def _normalize_meridian_env(env: dict[str, str]) -> None:
    """Normalize MERIDIAN_CONTEXT_*_DIR path overrides.

    Trims whitespace and drops blank placeholders.
    """
    import re

    context_pattern = re.compile(r"^MERIDIAN_CONTEXT_[A-Z][A-Z0-9_]*_DIR$")
    to_drop: list[str] = []
    for key in env:
        if key != "MERIDIAN_ACTIVE_WORK_DIR" and not context_pattern.match(key):
            continue
        normalized = env[key].strip()
        if normalized:
            env[key] = normalized
        else:
            to_drop.append(key)
    for key in to_drop:
        env.pop(key, None)


def sanitize_child_env(
    base_env: Mapping[str, str],
    env_overrides: Mapping[str, str] | None,
    pass_through: Collection[str],
) -> dict[str, str]:
    """Return a sanitized child environment with explicit pass-through controls."""

    pass_through_keys = {name.upper() for name in pass_through}
    sanitized: dict[str, str] = {}

    for key, value in base_env.items():
        normalized = key.upper()
        if _looks_like_secret_env_var(normalized) and normalized not in pass_through_keys:
            continue
        if normalized in pass_through_keys or _is_allowlisted_child_env_var(normalized):
            sanitized[key] = value

    if env_overrides is not None:
        sanitized.update(env_overrides)

    _normalize_meridian_env(sanitized)
    return sanitized


def inherit_child_env(
    base_env: Mapping[str, str],
    env_overrides: Mapping[str, str] | None,
    *,
    blocked: Collection[str] = BLOCKED_CHILD_ENV_VARS,
) -> dict[str, str]:
    """Return an inherited child environment with targeted non-propagation."""

    blocked_keys = {name.upper() for name in blocked}
    inherited = {key: value for key, value in base_env.items() if key.upper() not in blocked_keys}
    if env_overrides is not None:
        inherited.update(env_overrides)
    _normalize_meridian_env(inherited)
    return inherited


def build_harness_env_overrides(
    *,
    adapter: SubprocessHarness,
    run_params: SpawnParams,
    permission_config: PermissionConfig,
    runtime_env_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge runtime + adapter + MCP env overrides for one harness launch."""

    merged: dict[str, str] = dict(runtime_env_overrides or {})
    merged.update(adapter.env_overrides(permission_config))
    mcp_config = adapter.mcp_config(run_params)
    if mcp_config is not None:
        merged.update(mcp_config.env_overrides)
    if adapter.id == HarnessId.OPENCODE:
        if run_params.interactive:
            # Primary launch: let the OpenCode TUI handle permissions natively.
            merged.pop("OPENCODE_PERMISSION", None)
        elif "OPENCODE_PERMISSION" not in merged:
            # Child spawn with no explicit restriction: allow everything so
            # `opencode run` (subprocess mode) doesn't auto-reject all tool calls.
            merged["OPENCODE_PERMISSION"] = '{"*":"allow"}'
    if adapter.id == HarnessId.PI:
        merged["MERIDIAN_PI_SESSION_ROLE"] = resolve_pi_session_role(
            interactive=run_params.interactive
        )
    return merged


def resolve_pi_session_role(*, interactive: bool) -> PiSessionRole:
    """Resolve canonical Pi role used by both runtime env and manager policy."""

    return "primary" if interactive else "spawned"


def merge_env_overrides(
    *,
    plan_overrides: Mapping[str, str],
    runtime_overrides: Mapping[str, str],
    preflight_overrides: Mapping[str, str],
) -> dict[str, str]:
    """Merge env overrides and reject ``MERIDIAN_*`` leaks from plan/preflight.

    Keys in :data:`~meridian.lib.core.child_env.ALLOWED_CHILD_ENV_KEYS` must
    only enter the child environment via ``ChildEnvContext.child_context()``,
    which is in ``runtime_overrides``.  Any ``MERIDIAN_*`` key appearing in
    ``plan_overrides`` or ``preflight_overrides`` is a contract violation.
    """
    forbidden: list[tuple[str, str]] = []
    for key in plan_overrides:
        if key.startswith("MERIDIAN_"):
            forbidden.append((key, "plan_overrides"))
    for key in preflight_overrides:
        if key.startswith("MERIDIAN_"):
            forbidden.append((key, "preflight_overrides"))

    if forbidden:
        rendered = ", ".join(f"{key} via {source}" for key, source in sorted(forbidden))
        allowed_summary = ", ".join(sorted(ALLOWED_CHILD_ENV_KEYS))
        raise RuntimeError(
            "MERIDIAN_* keys may only be set by ChildEnvContext.child_context() "
            f"(allowed: {allowed_summary}); found leaks: {rendered}"
        )

    merged = dict(plan_overrides)
    merged.update(preflight_overrides)
    merged.update(runtime_overrides)
    return merged


def build_harness_child_env(
    *,
    base_env: Mapping[str, str],
    adapter: SubprocessHarness,
    run_params: SpawnParams,
    permission_config: PermissionConfig,
    runtime_env_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one inherited child env for a trusted harness launch."""

    merged_env = build_harness_env_overrides(
        adapter=adapter,
        run_params=run_params,
        permission_config=permission_config,
        runtime_env_overrides=runtime_env_overrides,
    )
    blocked_child_env_vars = getattr(adapter, "blocked_child_env_vars", None)
    adapter_blocked: frozenset[str]
    if callable(blocked_child_env_vars):
        adapter_blocked = cast("Callable[[], frozenset[str]]", blocked_child_env_vars)()
    else:
        adapter_blocked = cast("frozenset[str]", frozenset())
    return inherit_child_env(
        base_env=base_env,
        env_overrides=merged_env,
        blocked=BLOCKED_CHILD_ENV_VARS | adapter_blocked | RUNTIME_OVERRIDE_ENV_VARS,
    )


def build_env_plan(
    *,
    base_env: Mapping[str, str],
    adapter: SubprocessHarness,
    run_inputs: SpawnParams,
    permission_config: PermissionConfig,
    runtime_env_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Stage-owned entrypoint for launch child-environment construction."""

    return build_harness_child_env(
        base_env=base_env,
        adapter=adapter,
        run_params=run_inputs,
        permission_config=permission_config,
        runtime_env_overrides=runtime_env_overrides,
    )
