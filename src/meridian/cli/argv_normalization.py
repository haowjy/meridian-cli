"""Argv normalization helpers for optional-value session-initiation flags."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

SELF_FORK_REF_SENTINEL = "__SELF__"
SYNTHETIC_VALUE_TOKENS: frozenset[str] = frozenset({SELF_FORK_REF_SENTINEL})
FORK_IDENTITY_ERROR = (
    "--fork preserves launch identity. "
    "Use --fork-fresh to change agent, model, or skills."
)
FORK_INFERENCE_ERROR = (
    "Cannot infer --fork target: not inside a Meridian-managed session. "
    "Pass --fork REF explicitly."
)
FROM_INFERENCE_ERROR = (
    "Cannot infer --from target: not inside a Meridian-managed session. "
    "Pass --from REF explicitly."
)
_FORK_FRESH_CONFLICT = "Cannot combine --fork with --fork-fresh."
_FORK_CONTINUE_CONFLICT = "Cannot combine --fork with --continue."
_FORK_FRESH_CONTINUE_CONFLICT = "Cannot combine --fork-fresh with --continue."
_FROM_CONTINUE_CONFLICT = "Cannot combine --from with --continue."
_FORK_FROM_CONFLICT = "Cannot combine --fork with --from (MVP limitation)."
_FORK_FRESH_FROM_CONFLICT = "Cannot combine --fork-fresh with --from (MVP limitation)."
_OPTIONAL_VALUE_FLAGS = ("--fork", "--fork-fresh", "--from")


@dataclass(frozen=True)
class ForkModeResolution:
    """Result of resolving and validating fork/continue/from interactions."""

    fork_ref: str | None
    fork_fresh_ref: str | None
    is_fork: bool
    is_fresh: bool
    resolved_context_from: tuple[str, ...] = ()


def normalize_optional_value_flags(argv: Sequence[str]) -> list[str]:
    """Normalize optional-value flags so Cyclopts always sees a value token.

    Bare ``--fork``, ``--fork-fresh``, and ``--from`` are normalized to use
    ``SELF_FORK_REF_SENTINEL`` as an explicit value. ``--fork=value`` and
    ``--fork-fresh=value``/``--from=value`` are also normalized to separate
    flag/value tokens.
    """

    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            normalized.extend(argv[index:])
            break

        for flag in _OPTIONAL_VALUE_FLAGS:
            if token.startswith(f"{flag}="):
                value = token[len(flag) + 1 :]
                normalized.extend((flag, value if value else SELF_FORK_REF_SENTINEL))
                break
        else:
            if token in _OPTIONAL_VALUE_FLAGS:
                next_is_value = (
                    index + 1 < len(argv)
                    and argv[index + 1] != "--"
                    and not argv[index + 1].startswith("-")
                )
                normalized.append(token)
                if not next_is_value:
                    normalized.append(SELF_FORK_REF_SENTINEL)
                index += 1
                continue

            normalized.append(token)
            index += 1
            continue

        index += 1

    return normalized


def resolve_optional_ref(raw_ref: str | None, *, flag_name: str = "--fork") -> str | None:
    """Resolve normalized optional-value flag values from environment."""

    if raw_ref is None:
        return None
    normalized = raw_ref.strip()
    if not normalized:
        return None
    if normalized != SELF_FORK_REF_SENTINEL:
        return normalized

    spawn_id = os.environ.get("MERIDIAN_SPAWN_ID", "").strip()
    if not spawn_id:
        if flag_name == "--fork":
            raise ValueError(FORK_INFERENCE_ERROR)
        if flag_name == "--from":
            raise ValueError(FROM_INFERENCE_ERROR)
        raise ValueError(
            f"Cannot infer {flag_name} target: not inside a Meridian-managed session. "
            f"Pass {flag_name} REF explicitly."
        )
    return spawn_id


def resolve_fork_ref(raw_ref: str | None) -> str | None:
    """Legacy wrapper. Prefer resolve_optional_ref."""

    return resolve_optional_ref(raw_ref, flag_name="--fork")


def validate_fork_mode(
    *,
    fork_from: str | None,
    fork_fresh_from: str | None,
    continue_from: str | None,
    context_from: tuple[str, ...] | Sequence[str] = (),
    agent: str | None = None,
    model: str = "",
    skills: str | None = None,
) -> ForkModeResolution:
    """Validate fork/continue/from mutual exclusions and resolve refs."""

    if fork_from is not None and fork_fresh_from is not None:
        raise ValueError(_FORK_FRESH_CONFLICT)
    if fork_from is not None and continue_from is not None:
        raise ValueError(_FORK_CONTINUE_CONFLICT)
    if fork_fresh_from is not None and continue_from is not None:
        raise ValueError(_FORK_FRESH_CONTINUE_CONFLICT)
    if context_from and continue_from is not None:
        raise ValueError(_FROM_CONTINUE_CONFLICT)

    if fork_from is not None and context_from:
        raise ValueError(_FORK_FROM_CONFLICT)
    if fork_fresh_from is not None and context_from:
        raise ValueError(_FORK_FRESH_FROM_CONFLICT)

    if (
        fork_from is not None
        and ((agent is not None and agent.strip()) or model.strip() or skills is not None)
    ):
        raise ValueError(FORK_IDENTITY_ERROR)

    resolved_context_from = (
        tuple(
            resolve_optional_ref(ref, flag_name="--from") or ref
            for ref in context_from
        )
        if context_from
        else ()
    )
    resolved_fork = resolve_optional_ref(fork_from, flag_name="--fork")
    resolved_fork_fresh = resolve_optional_ref(fork_fresh_from, flag_name="--fork-fresh")
    return ForkModeResolution(
        fork_ref=resolved_fork,
        fork_fresh_ref=resolved_fork_fresh,
        is_fork=resolved_fork is not None or resolved_fork_fresh is not None,
        is_fresh=resolved_fork_fresh is not None,
        resolved_context_from=resolved_context_from,
    )
