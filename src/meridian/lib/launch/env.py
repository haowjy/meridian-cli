"""Child-process environment helpers shared by launch and spawn paths."""

from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.child_env import ALLOWED_CHILD_ENV_KEYS
from meridian.lib.core.overrides import RUNTIME_OVERRIDE_ENV_VARS
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.connections.base import PiSessionRole
from meridian.lib.safety.permissions import PermissionConfig

from .constants import BLOCKED_CHILD_ENV_VARS
from .env_sanitize import (
    ChildEnvPolicy,
    build_connection_child_env,
    build_meridian_subprocess_env,
    child_env_policy_from_config,
    collect_child_env_passthrough,
    inherit_child_env,
    sanitize_child_env,
)

_PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"

__all__ = [
    "ChildEnvPolicy",
    "build_connection_child_env",
    "build_env_plan",
    "build_harness_child_env",
    "build_harness_env_overrides",
    "build_meridian_subprocess_env",
    "child_env_policy_from_config",
    "collect_child_env_passthrough",
    "inherit_child_env",
    "merge_env_overrides",
    "resolve_pi_session_role",
    "sanitize_child_env",
    "scope_pi_session_dir_for_spawn",
]


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


def scope_pi_session_dir_for_spawn(
    *,
    child_env: dict[str, str],
    spawn_id: SpawnId,
) -> str | None:
    """Scope Pi session storage to one spawn, idempotently."""

    session_root = child_env.get(_PI_SESSION_DIR_ENV, "").strip()
    if not session_root:
        return None

    resolved_spawn_id = str(spawn_id).strip()
    if not resolved_spawn_id:
        return None

    scoped_root = Path(session_root).expanduser()
    if scoped_root.name != resolved_spawn_id:
        scoped_root = scoped_root / resolved_spawn_id
    scoped_root.mkdir(parents=True, exist_ok=True)
    child_env[_PI_SESSION_DIR_ENV] = str(scoped_root)
    return str(scoped_root)


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
        if key.upper().startswith("MERIDIAN_"):
            forbidden.append((key, "plan_overrides"))
    for key in preflight_overrides:
        if key.upper().startswith("MERIDIAN_"):
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
    env_policy: ChildEnvPolicy | None = None,
    explicit_env_grants: Collection[str] = (),
) -> dict[str, str]:
    """Build one child env for a harness launch."""

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
    blocked = BLOCKED_CHILD_ENV_VARS | adapter_blocked | RUNTIME_OVERRIDE_ENV_VARS
    if env_policy is not None and env_policy.inherit_full_env:
        return inherit_child_env(
            base_env=base_env,
            env_overrides=merged_env,
            blocked=blocked,
        )
    passthrough = collect_child_env_passthrough(
        harness_passthrough=adapter.contract.coordinator_env_passthrough,
        policy=env_policy,
        explicit_grants=explicit_env_grants,
    )
    return sanitize_child_env(
        base_env=base_env,
        env_overrides=merged_env,
        pass_through=passthrough,
    )


def build_env_plan(
    *,
    base_env: Mapping[str, str],
    adapter: SubprocessHarness,
    run_inputs: SpawnParams,
    permission_config: PermissionConfig,
    runtime_env_overrides: Mapping[str, str] | None = None,
    env_policy: ChildEnvPolicy | None = None,
    explicit_env_grants: Collection[str] = (),
) -> dict[str, str]:
    """Stage-owned entrypoint for launch child-environment construction."""

    return build_harness_child_env(
        base_env=base_env,
        adapter=adapter,
        run_params=run_inputs,
        permission_config=permission_config,
        runtime_env_overrides=runtime_env_overrides,
        env_policy=env_policy,
        explicit_env_grants=explicit_env_grants,
    )
