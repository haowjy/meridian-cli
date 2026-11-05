"""The line that tells a user how to relocate a chat whose directory or store moved.

Every launch and read path that finds a moved path prints the same hint, built here from
the chat's recorded state: ``--from`` comes from ``moved_root``, ``--to`` is the project
root the invocation resolved.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from meridian.lib.core.native_identity import NativeSessionUnavailable, NativeStoreMove
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.ops.session_relocate import moved_root
from meridian.lib.state import session_store


def relocate_hint(
    *,
    runtime_root: Path,
    chat_id: str,
    target: Path,
    store_move: NativeStoreMove | None = None,
    only_if_gone: bool = False,
) -> str | None:
    """How to move ``chat_id`` to ``target``, or None when nothing can be said.

    ``only_if_gone`` limits the hint to a recorded directory that no longer exists, for
    reads, which never launch from a second checkout.
    """
    generations = session_store.chat_generations(runtime_root, chat_id)
    if not generations:
        return None
    adapter = get_default_harness_registry().get_subprocess_harness(
        HarnessId(generations[-1].harness)
    )
    lines: list[str] = []
    if store_move is not None and store_move.kind == "config_root":
        root = Path(store_move.recorded).parent.parent
        lines.append(
            f"{chat_id} is recorded under the Claude config root {root}: "
            f"set CLAUDE_CONFIG_DIR={root}"
        )
        if Path(store_move.recorded).name == Path(store_move.attempted).name:
            return "\n".join(lines)
        lines.append("The project directory moved as well:")
    old = moved_root(generations, adapter, target=target)
    if only_if_gone and old is not None and old.exists():
        return None
    if old is None:
        lines.append(f"meridian session relocate {chat_id}")
    elif old.exists():
        lines.append(
            f"{chat_id} is recorded in {old}, you're in {target}: continue from there, or "
            f"run `meridian session relocate {chat_id}` to move it here."
        )
    else:
        lines.append(
            f"{old} is gone: meridian session relocate --from {shlex.quote(str(old))} "
            f"--to {shlex.quote(str(target))}"
        )
    return "\n".join(lines)


def with_relocate_hint(
    exc: NativeSessionUnavailable,
    *,
    runtime_root: Path,
    chat_id: str | None,
    target: Path,
) -> NativeSessionUnavailable:
    """``exc`` with the relocate hint a ``store_moved`` refusal carries."""
    if exc.reason != "store_moved" or chat_id is None:
        return exc
    hint = relocate_hint(
        runtime_root=runtime_root, chat_id=chat_id, target=target, store_move=exc.store_move
    )
    return exc.with_hint(hint) if hint else exc


def with_read_hint(
    exc: NativeSessionUnavailable, *, project_root: Path
) -> NativeSessionUnavailable:
    """``exc`` with the relocate hint for a transcript read whose recorded directory is
    gone. Reads never search or launch, so only a gone directory gets a hint."""
    runtime_root = resolve_runtime_root_for_read(project_root)
    if exc.reason != "missing" or runtime_root is None:
        return exc
    hint = relocate_hint(
        runtime_root=runtime_root, chat_id=exc.ref, target=project_root, only_if_gone=True
    )
    return exc.with_hint(hint) if hint else exc
