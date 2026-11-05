"""Read-only questions about a chat whose directory or store may have moved.

The launch and read paths ask them; ``meridian session relocate`` is the one thing that
acts. ``needs_relocate`` asks whether the next launch would fail because the harness's
own record points at a gone directory (a moved store is finalize's check, not this
one); ``moved_root`` asks where the chat moved from; ``relocate_hint`` turns the answer
into the line a user runs. A missing Meridian task dir (spawn ``--continue``) is not a
gate reason: it fails before launch on every harness and gets a hint instead.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from meridian.lib.core.native_identity import (
    NativeKeyFields,
    NativeSessionUnavailable,
    NativeStoreMove,
)
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_store
from meridian.lib.state.paths import rebase_path
from meridian.lib.state.session_fold import SessionRecord, SessionRelocateEvent


def adapter_for(harness: str) -> SubprocessHarness | None:
    """The adapter for a record's harness; None for a harness this build has no adapter
    for, so every caller treats that record the same way: nothing harness-side to do."""
    try:
        return get_default_harness_registry().get_subprocess_harness(HarnessId(harness))
    except KeyError:
        return None


def chat_key(generations: Sequence[SessionRecord]) -> NativeKeyFields:
    """The chat's native key from its generations, oldest first: the newest bound one."""
    for generation in reversed(generations):
        if generation.native_key() is not None:
            return generation.key_fields()
    return generations[-1].key_fields() if generations else NativeKeyFields()


def control_roots(generations: Sequence[SessionRecord]) -> tuple[Path, ...]:
    """The chat's control roots, newest first."""
    return tuple(Path(g.control_root) for g in reversed(generations) if g.control_root)


def harness_cwd(generations: Sequence[SessionRecord], *, skip: Path | None = None) -> Path | None:
    """The directory the chat's harness records for it, or None when it keeps none.
    A recorded root equal to ``skip`` is not offered to the harness as evidence."""
    adapter = adapter_for(generations[-1].harness) if generations else None
    if adapter is None:
        return None
    roots = tuple(root for root in control_roots(generations) if root != skip)
    return adapter.native_cwd(chat_key(generations), recorded_roots=roots)


def needs_relocate(generations: Sequence[SessionRecord]) -> Path | None:
    """The gate: the directory the harness records for this chat when it is gone, so
    the next launch would fail on it. ``generations`` are the chat's
    (``session_store.chat_generations``)."""
    resumes_in = harness_cwd(generations)
    return resumes_in if resumes_in is not None and not resumes_in.is_dir() else None


def refused_relocation(
    runtime_root: Path, generations: Sequence[SessionRecord], *, target: Path
) -> str | None:
    """The gate's other state: the chat's newest relocate already moved it to ``target``,
    yet the harness still records that relocate's old directory.

    Relocate writes the chat's Meridian paths even when the harness refuses its own
    change, so callers show this instead of offering relocate again, which would loop.
    None when no such relocate exists or the harness no longer records its old place.
    """
    chat_id = generations[-1].chat_id
    moves = [
        row.paths
        for row in session_store.chat_rows(runtime_root, chat_id)
        if isinstance(row, SessionRelocateEvent) and row.paths is not None
    ]
    adapter = adapter_for(generations[-1].harness)
    if adapter is None or not moves or not target.is_relative_to(moves[-1].new):
        return None
    move = moves[-1]
    cwd_from = harness_cwd(generations)
    if cwd_from is None:
        return None
    moved = rebase_path(str(cwd_from), move.olds, move.new)
    if moved is None or not Path(moved).is_dir():
        return None
    plan = adapter.plan_native_relocate(
        chat_key(generations), cwd_from=cwd_from, cwd_to=Path(moved)
    )
    if plan is None:
        return None
    reason = plan.refusal or f"rerun `meridian session relocate {chat_id}` to finish"
    return (
        f"{chat_id} was relocated to {move.new}, but {adapter.id} still records "
        f"{move.old}: {reason}"
    )


def moved_root(
    runtime_root: Path, generations: Sequence[SessionRecord], *, target: Path
) -> Path | None:
    """The directory this chat moved from to ``target``: relocate's default ``--from``.

    The harness's own record wins, then each generation newest first, by ``task_cwd``,
    ``execution_cwd``, ``control_root``, then the newest relocate row's ``old`` (a rerun
    after every record moved). Any recorded path equal to ``target`` is skipped: a
    ``--continue`` from the new directory, by a build without the gate, appended a start
    that records it.
    """
    recorded = harness_cwd(generations, skip=target)
    if recorded is not None and recorded != target:
        return recorded
    for generation in reversed(generations):
        # Legacy rows carry no control_root.
        for field in (generation.task_cwd, generation.execution_cwd, generation.control_root):
            if field and Path(field) != target:
                return Path(field)
    if generations:
        for row in reversed(session_store.chat_rows(runtime_root, generations[-1].chat_id)):
            if isinstance(row, SessionRelocateEvent) and row.paths is not None:
                return Path(row.paths.old) if Path(row.paths.old) != target else None
    return None


def relocate_hint(
    *,
    runtime_root: Path,
    chat_id: str,
    target: Path,
    store_move: NativeStoreMove | None = None,
    only_if_gone: bool = False,
) -> str | None:
    """How to move ``chat_id`` to ``target``, or None when nothing can be said.

    ``--from`` comes from ``moved_root``, ``--to`` is the project root the invocation
    resolved. ``only_if_gone`` limits the hint to a recorded directory that no longer
    exists, for reads, which never launch from a second checkout.
    """
    generations = session_store.chat_generations(runtime_root, chat_id)
    if not generations:
        return None
    lines: list[str] = []
    if store_move is not None and store_move.fix is not None:
        lines.append(f"{chat_id} {store_move.fix}")
        if not store_move.also_moved:
            return "\n".join(lines)
        lines.append("The project directory moved as well:")
    old = moved_root(runtime_root, generations, target=target)
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


def copy_checkout_note(
    *,
    runtime_root: Path,
    chat_id: str | None,
    task_dir: str | None,
    project_root: Path,
) -> str | None:
    """The note for a continue that resumes in another checkout of the project.

    A harness that resumes in the directory the chat recorded runs a continue from a
    copy in the original while it still exists; one that resumes in its source store is
    covered by its store check. No note when the resume directory is the project root or
    under it. Print only: the user may well want the original.
    """
    if chat_id is None or not task_dir or not Path(task_dir).is_dir():
        return None
    generations = session_store.chat_generations(runtime_root, chat_id)
    adapter = adapter_for(generations[-1].harness) if generations else None
    if (
        adapter is None
        or "resume" not in adapter.continues_in_source_store
        or Path(task_dir).resolve().is_relative_to(project_root)
    ):
        return None
    return (
        f"{chat_id} resumes in {task_dir} (recorded); you're in {project_root}. "
        f"To move it here: meridian session relocate {chat_id}"
    )


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
