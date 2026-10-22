"""Child-process environment helpers shared by launch and spawn paths."""

import json
import math
import shutil
import subprocess
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.child_env import ALLOWED_CHILD_ENV_KEYS
from meridian.lib.core.overrides import RUNTIME_OVERRIDE_ENV_VARS
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.connections.base import PiSessionRole

from .constants import BLOCKED_CHILD_ENV_VARS

_PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_PI_CHILD_WAVE_TIMEOUT_MS_ENV = "_MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS"
_PI_TASK_PING_INTERVAL_MS_ENV = "_MERIDIAN_PI_TASK_PING_INTERVAL_MS"
_PI_TASK_PING_RESET_ON_ACTIVITY_ENV = "_MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY"


def undo_direnv(base_env: Mapping[str, str]) -> dict[str, str]:
    """Ask direnv to reverse its diff without evaluating shell output or loading an rc.

    The filesystem root has no ancestors to search. Refuse a root rc rather
    than accidentally load another environment. Never expose captured env or
    direnv stderr in an exception: either can contain credentials.
    """
    if not base_env.get("DIRENV_DIFF"):
        return dict(base_env)
    root = Path("/")
    if any((root / name).exists() for name in (".envrc", ".env")):
        raise ValueError("Cannot ignore direnv: the filesystem root contains an environment file.")
    executable = shutil.which("direnv", path=base_env.get("PATH", ""))
    if executable is None:
        raise ValueError("Cannot ignore direnv: DIRENV_DIFF is set but direnv is not on PATH.")
    executable = str(Path(executable).resolve())
    try:
        result = subprocess.run(
            [executable, "exec", str(root), executable, "dump", "json"],
            env=dict(base_env),
            capture_output=True,
            check=True,
            timeout=10,
        )
        restored = json.loads(result.stdout)
        if not isinstance(restored, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in cast("dict[object, object]", restored).items()
        ):
            raise ValueError("Invalid direnv environment")
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("Cannot ignore direnv: environment restoration failed.") from None
    return cast("dict[str, str]", restored)


def apply_pi_bind_time_env(
    env: dict[str, str],
    *,
    launch_role: PiSessionRole,
    timeout_seconds: float | None,
    interval_seconds: float | None,
    reset_on_activity: bool | None,
) -> None:
    """Bake resolved Pi runtime settings into a complete child environment."""

    if (
        launch_role == "spawned"
        and timeout_seconds is not None
        and math.isfinite(timeout_seconds)
        and timeout_seconds > 0
    ):
        env[_PI_CHILD_WAVE_TIMEOUT_MS_ENV] = str(max(1, int(timeout_seconds * 1000)))
    if (
        interval_seconds is not None
        and math.isfinite(interval_seconds)
        and interval_seconds > 0
    ):
        env[_PI_TASK_PING_INTERVAL_MS_ENV] = str(max(1, int(interval_seconds * 1000)))
    if reset_on_activity is not None:
        env[_PI_TASK_PING_RESET_ON_ACTIVITY_ENV] = "true" if reset_on_activity else "false"


def _normalize_meridian_env(env: dict[str, str]) -> None:
    """Normalize MERIDIAN_CONTEXT_*_DIR path overrides.

    Trims whitespace and drops blank placeholders.
    """
    import re

    context_pattern = re.compile(r"^MERIDIAN_CONTEXT_[A-Z][A-Z0-9_]*_DIR$")
    to_drop: list[str] = []
    normalize_keys = ("MERIDIAN_ACTIVE_WORK_DIR", "MERIDIAN_PROJECT_DIR", "MERIDIAN_TASK_DIR")
    for key in env:
        if key not in normalize_keys and not context_pattern.match(key):
            continue
        normalized = env[key].strip()
        if normalized:
            env[key] = normalized
        else:
            to_drop.append(key)
    for key in to_drop:
        env.pop(key, None)


def inherit_child_env(
    base_env: Mapping[str, str],
    env_overrides: Mapping[str, str] | None,
    *,
    blocked: Collection[str] = BLOCKED_CHILD_ENV_VARS,
) -> dict[str, str]:
    """Return an inherited child environment with targeted non-propagation."""

    blocked_keys = {name.upper() for name in blocked}
    inherited = {
        key: value
        for key, value in base_env.items()
        if key.upper() not in blocked_keys
        and not key.upper().startswith("MERIDIAN_SECRET_")
    }
    if env_overrides is not None:
        inherited.update(env_overrides)
    _normalize_meridian_env(inherited)
    return inherited


def build_harness_env_overrides(
    *,
    adapter: SubprocessHarness,
    run_params: SpawnParams,
    runtime_env_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge runtime + MCP env overrides for one harness launch."""

    merged: dict[str, str] = dict(runtime_env_overrides or {})
    mcp_config = adapter.mcp_config(run_params)
    if mcp_config is not None:
        merged.update(mcp_config.env_overrides)
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
        if key.upper().startswith(("MERIDIAN_", "_MERIDIAN_")):
            forbidden.append((key, "plan_overrides"))
    for key in preflight_overrides:
        if key.upper().startswith(("MERIDIAN_", "_MERIDIAN_")):
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
    runtime_env_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one inherited child env for a trusted harness launch."""

    merged_env = build_harness_env_overrides(
        adapter=adapter,
        run_params=run_params,
        runtime_env_overrides=runtime_env_overrides,
    )
    blocked_child_env_vars = getattr(adapter, "blocked_child_env_vars", None)
    adapter_blocked: frozenset[str]
    if callable(blocked_child_env_vars):
        adapter_blocked = cast("Callable[[], frozenset[str]]", blocked_child_env_vars)()
    else:
        adapter_blocked = cast("frozenset[str]", frozenset())
    child_env = inherit_child_env(
        base_env=base_env,
        env_overrides=merged_env,
        blocked=BLOCKED_CHILD_ENV_VARS | adapter_blocked | RUNTIME_OVERRIDE_ENV_VARS,
    )
    for key, value in adapter.env_defaults(run=run_params, env=child_env).items():
        if not child_env.get(key, "").strip():
            child_env[key] = value
    return child_env


def build_env_plan(
    *,
    base_env: Mapping[str, str],
    adapter: SubprocessHarness,
    run_inputs: SpawnParams,
    runtime_env_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Stage-owned entrypoint for launch child-environment construction."""

    return build_harness_child_env(
        base_env=base_env,
        adapter=adapter,
        run_params=run_inputs,
        runtime_env_overrides=runtime_env_overrides,
    )
