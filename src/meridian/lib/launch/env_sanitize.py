"""Sanitized child-environment primitives without harness adapter imports."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import cast

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.harness_env_passthrough import (
    HarnessEnvPassthrough,
    harness_env_passthrough,
)

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


def _matches_passthrough(key: str, passthrough: HarnessEnvPassthrough) -> bool:
    normalized = key.upper()
    if normalized in passthrough.exact:
        return True
    return any(normalized.startswith(prefix) for prefix in passthrough.prefixes)


@dataclass(frozen=True)
class ChildEnvPolicy:
    """Project-level child-env policy loaded from ``[spawn]`` config."""

    inherit_full_env: bool = False
    extra_passthrough: frozenset[str] = frozenset()


def collect_child_env_passthrough(
    *,
    harness_id: HarnessId,
    policy: ChildEnvPolicy | None = None,
    explicit_grants: Collection[str] = (),
) -> HarnessEnvPassthrough:
    """Merge harness, config, and explicit env-grant passthrough declarations."""

    harness = harness_env_passthrough(harness_id)
    extra_exact = {name.upper() for name in explicit_grants}
    if policy is not None:
        extra_exact.update(policy.extra_passthrough)
    return HarnessEnvPassthrough(
        exact=harness.exact | extra_exact,
        prefixes=harness.prefixes,
    )


def _normalize_meridian_env(env: dict[str, str]) -> None:
    """Normalize MERIDIAN_CONTEXT_*_DIR path overrides.

    Trims whitespace and drops blank placeholders.
    """
    import re

    context_pattern = re.compile(r"^MERIDIAN_CONTEXT_[A-Z][A-Z0-9_]*_DIR$")
    to_drop: list[str] = []
    normalize_keys = ("MERIDIAN_ACTIVE_WORK_DIR", "MERIDIAN_PROJECT_ROOT", "MERIDIAN_TASK_DIR")
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


def sanitize_child_env(
    base_env: Mapping[str, str],
    env_overrides: Mapping[str, str] | None,
    pass_through: Collection[str] | HarnessEnvPassthrough,
) -> dict[str, str]:
    """Return a sanitized child environment with explicit pass-through controls."""

    if isinstance(pass_through, HarnessEnvPassthrough):
        passthrough = pass_through
    else:
        passthrough = HarnessEnvPassthrough(
            exact=frozenset(name.upper() for name in pass_through),
        )
    sanitized: dict[str, str] = {}

    for key, value in base_env.items():
        normalized = key.upper()
        if normalized.startswith("MERIDIAN_"):
            continue
        if _looks_like_secret_env_var(normalized) and not _matches_passthrough(
            normalized, passthrough
        ):
            continue
        if _matches_passthrough(normalized, passthrough) or _is_allowlisted_child_env_var(
            normalized
        ):
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


def build_connection_child_env(
    *,
    harness_id: HarnessId,
    base_env: Mapping[str, str],
    env_overrides: Mapping[str, str] | None,
    env_policy: ChildEnvPolicy | None = None,
    explicit_env_grants: Collection[str] = (),
    blocked: Collection[str] = BLOCKED_CHILD_ENV_VARS,
) -> dict[str, str]:
    """Build sanitized child env for one harness connection transport."""

    if env_policy is not None and env_policy.inherit_full_env:
        return inherit_child_env(
            base_env=base_env,
            env_overrides=env_overrides,
            blocked=blocked,
        )
    passthrough = collect_child_env_passthrough(
        harness_id=harness_id,
        policy=env_policy,
        explicit_grants=explicit_env_grants,
    )
    return sanitize_child_env(
        base_env=base_env,
        env_overrides=env_overrides,
        pass_through=passthrough,
    )


def build_meridian_subprocess_env(
    *,
    base_env: Mapping[str, str],
    env_overrides: Mapping[str, str] | None = None,
    env_policy: ChildEnvPolicy | None = None,
) -> dict[str, str]:
    """Build sanitized env for Meridian-to-Meridian subprocesses (e.g. bg workers)."""

    if env_policy is not None and env_policy.inherit_full_env:
        return inherit_child_env(
            base_env=base_env,
            env_overrides=env_overrides,
            blocked=BLOCKED_CHILD_ENV_VARS,
        )
    passthrough = HarnessEnvPassthrough(
        exact=env_policy.extra_passthrough if env_policy is not None else frozenset(),
    )
    return sanitize_child_env(
        base_env=base_env,
        env_overrides=env_overrides,
        pass_through=passthrough,
    )


def child_env_policy_from_config(config: object) -> ChildEnvPolicy:
    """Extract spawn child-env policy from one ``MeridianConfig`` instance."""

    inherit_full_env = bool(getattr(config, "inherit_full_env", False))
    raw_passthrough = getattr(config, "env_passthrough", ())
    extra = frozenset(
        name.strip().upper()
        for name in cast("tuple[str, ...]", raw_passthrough or ())
        if name.strip()
    )
    return ChildEnvPolicy(
        inherit_full_env=inherit_full_env,
        extra_passthrough=extra,
    )


__all__ = [
    "ChildEnvPolicy",
    "build_connection_child_env",
    "build_meridian_subprocess_env",
    "child_env_policy_from_config",
    "collect_child_env_passthrough",
    "inherit_child_env",
    "sanitize_child_env",
]
