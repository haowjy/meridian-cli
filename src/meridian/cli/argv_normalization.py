"""Argv normalization helpers for optional-value fork flags."""

from __future__ import annotations

import os
from collections.abc import Sequence

SELF_FORK_REF_SENTINEL = "__SELF__"
FORK_INFERENCE_ERROR = (
    "Cannot infer --fork target: not inside a Meridian-managed session. "
    "Pass --fork REF explicitly."
)
_OPTIONAL_FORK_VALUE_FLAGS = ("--fork", "--fork-fresh")


def normalize_optional_value_flags(argv: Sequence[str]) -> list[str]:
    """Normalize optional-value fork flags so Cyclopts always sees a value token.

    Bare ``--fork`` and ``--fork-fresh`` are normalized to use
    ``SELF_FORK_REF_SENTINEL`` as an explicit value. ``--fork=value`` and
    ``--fork-fresh=value`` are also normalized to separate flag/value tokens.
    """

    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            normalized.extend(argv[index:])
            break

        for flag in _OPTIONAL_FORK_VALUE_FLAGS:
            if token.startswith(f"{flag}="):
                value = token[len(flag) + 1 :]
                normalized.extend((flag, value if value else SELF_FORK_REF_SENTINEL))
                break
        else:
            if token in _OPTIONAL_FORK_VALUE_FLAGS:
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


def resolve_fork_ref(raw_ref: str | None) -> str | None:
    """Resolve normalized fork values, expanding self-sentinel from environment."""

    if raw_ref is None:
        return None
    normalized = raw_ref.strip()
    if not normalized:
        return None
    if normalized != SELF_FORK_REF_SENTINEL:
        return normalized

    spawn_id = os.environ.get("MERIDIAN_SPAWN_ID", "").strip()
    if not spawn_id:
        raise ValueError(FORK_INFERENCE_ERROR)
    return spawn_id
