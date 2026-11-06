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
from meridian.lib.state.session_fold import PathMove, SessionRecord, SessionRelocateEvent


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


def _control_roots(generations: Sequence[SessionRecord]) -> tuple[Path, ...]:
    """The chat's control roots, newest first."""
    return tuple(Path(g.control_root) for g in reversed(generations) if g.control_root)


def harness_cwd(generations: Sequence[SessionRecord], *, skip: Path | None = None) -> Path | None:
    """The directory the chat's harness records for it, or None when it keeps none.
    A recorded root equal to ``skip`` is not offered to the harness as evidence."""
    adapter = adapter_for(generations[-1].harness) if generations else None
    if adapter is None:
        return None
    roots = tuple(root for root in _control_roots(generations) if root != skip)
    return adapter.native_cwd(chat_key(generations), recorded_roots=roots)


def needs_relocate(generations: Sequence[SessionRecord]) -> Path | None:
    """The gate: the directory the harness records for this chat when it is gone, so
    the next launch would fail on it. ``generations`` are the chat's
    (``session_store.chat_generations``)."""
    resumes_in = harness_cwd(generations)
    return resumes_in if resumes_in is not None and not resumes_in.is_dir() else None


def _relocations(runtime_root: Path, chat_id: str) -> list[PathMove]:
    """The directory moves the chat's relocate rows recorded, oldest first."""
    return [
        row.paths
        for row in session_store.chat_rows(runtime_root, chat_id)
        if isinstance(row, SessionRelocateEvent) and row.paths is not None
    ]


def _pinned_dir(generations: Sequence[SessionRecord]) -> Path | None:
    """The directory the chat is pinned to on purpose (a worktree: newest ``task_cwd``
    differs from its ``control_root``), or None for an unpinned chat."""
    newest = generations[-1]
    if newest.task_cwd and newest.control_root and newest.task_cwd != newest.control_root:
        return Path(newest.task_cwd)
    return None


def refused_relocation(
    runtime_root: Path,
    generations: Sequence[SessionRecord],
    *,
    target: Path,
    moves: Sequence[PathMove] | None = None,
) -> str | None:
    """The gate's other state: the chat's newest relocate already moved it to ``target``,
    yet the harness still records that relocate's old directory.

    Relocate writes the chat's Meridian paths even when the harness refuses its own
    change, so callers show this instead of offering relocate again, which would loop.
    None when no such relocate exists or the harness no longer records its old place.
    ``moves`` are the chat's relocations when the caller already read them.
    """
    chat_id = generations[-1].chat_id
    if moves is None:
        moves = _relocations(runtime_root, chat_id)
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
    runtime_root: Path,
    generations: Sequence[SessionRecord],
    *,
    target: Path,
    moves: Sequence[PathMove] | None = None,
) -> Path | None:
    """The directory this chat moved from to ``target``: relocate's default ``--from``.

    Candidates are the harness's own record, then the recorded ``control_root``s, newest
    first. ``task_cwd`` and ``execution_cwd`` are never candidates, and neither is the
    directory a worktree chat is pinned to, which the harness records too: ``target`` is
    the invocation's project root, never where a worktree went, so a worktree that itself
    moved needs an explicit ``--from`` and ``--to``. A candidate equal to
    ``target`` is skipped (a ``--continue`` from the new
    directory, by a build without the gate, appended a start that records it), and so is
    one a relocate row already moved to a directory holding ``target``: that move is
    applied, and a stale harness record still naming it is not a new one.
    """
    if not generations:
        return None
    if moves is None:
        moves = _relocations(runtime_root, generations[-1].chat_id)
    applied = [move for move in moves if target.is_relative_to(move.new)]
    pinned = _pinned_dir(generations)
    candidates = (
        harness_cwd(generations, skip=target),
        *_control_roots(generations),
    )
    for candidate in candidates:
        if candidate is None or candidate in (target, pinned):
            continue
        if any(rebase_path(str(candidate), move.olds, move.new) is not None for move in applied):
            continue
        return candidate
    return None


def _recorded_dir(generations: Sequence[SessionRecord]) -> Path | None:
    """The newest directory the chat's own records name: its ``task_cwd``, else its
    ``control_root``. What a read hint tests for existence, not the harness's record."""
    newest = generations[-1]
    path = newest.task_cwd or newest.control_root
    return Path(path) if path else None


def _gone_dir(generations: Sequence[SessionRecord]) -> Path | None:
    """A directory the chat records, the harness's first, that no longer exists."""
    return next(
        (d for d in (harness_cwd(generations), _recorded_dir(generations)) if d and not d.exists()),
        None,
    )


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
    exists, for reads, which never launch from a second checkout, and for a transcript that is
    missing, which a live directory does not explain.
    """
    generations = session_store.chat_generations(runtime_root, chat_id)
    if not generations:
        return None
    moves = _relocations(runtime_root, chat_id)
    # Already relocated, harness refused: relocating again would loop.
    if (
        refused := refused_relocation(runtime_root, generations, target=target, moves=moves)
    ) is not None:
        return refused
    lines: list[str] = []
    if store_move is not None and store_move.fix is not None:
        lines.append(f"{chat_id} {store_move.fix}")
        if not store_move.also_moved:
            return "\n".join(lines)
        lines.append("The project directory moved as well:")
    if only_if_gone and ((recorded := _recorded_dir(generations)) is None or recorded.exists()):
        return None
    old = moved_root(runtime_root, generations, target=target, moves=moves)
    if old is not None and old.exists():
        lines.append(
            f"{chat_id} is recorded in {old}, you're in {target}: continue from there, or "
            f"run `meridian session relocate {chat_id}` to move it here."
        )
    elif old is not None:
        lines.append(
            f"{old} is gone: meridian session relocate --from {shlex.quote(str(old))} "
            f"--to {shlex.quote(str(target))}"
        )
    elif (gone := _gone_dir(generations)) is not None:
        # Only one end is known, so print the explicit form with a blank for the other.
        lines.append(
            f"{chat_id}'s directory {gone} is gone; if it moved: meridian session "
            f"relocate {chat_id} --from {shlex.quote(str(gone))} --to <where it is now>"
        )
    elif store_move is not None and store_move.fix is not None:
        lines.pop()  # the header promised a move this hint cannot name
    return "\n".join(lines) or None


def copy_checkout_note(
    *, runtime_root: Path, chat_id: str | None, project_root: Path
) -> str | None:
    """The note for a continue run from a second checkout of the project.

    A harness that resumes in the directory the chat recorded runs a continue from a
    copy in the original while it still exists; one that resumes in its source store is
    covered by its store check. Only an unpinned chat qualifies (``task_cwd`` is its
    ``control_root``): a worktree chat resumes in its worktree on purpose, and relocating
    it would pull it out. Print only: the user may well want the original.
    """
    if chat_id is None:
        return None
    generations = session_store.chat_generations(runtime_root, chat_id)
    adapter = adapter_for(generations[-1].harness) if generations else None
    if adapter is None or "resume" not in adapter.continues_in_source_store:
        return None
    newest = generations[-1]
    if not newest.control_root or _pinned_dir(generations) is not None:
        return None
    recorded = Path(newest.control_root)
    if not recorded.is_dir() or recorded.resolve() == project_root.resolve():
        return None
    return (
        f"{chat_id} resumes in {recorded} (recorded); you're in {project_root}. "
        f"To move it here: meridian session relocate {chat_id}"
    )


def with_relocate_hint(
    exc: NativeSessionUnavailable,
    *,
    runtime_root: Path,
    chat_id: str | None,
    target: Path,
) -> NativeSessionUnavailable:
    """``exc`` with the relocate hint a ``store_moved`` refusal carries, or a ``missing``
    one when the chat's recorded directory is gone (a live one says nothing of a move)."""
    if exc.reason not in ("store_moved", "missing") or chat_id is None:
        return exc
    hint = relocate_hint(
        runtime_root=runtime_root,
        chat_id=chat_id,
        target=target,
        store_move=exc.store_move,
        only_if_gone=exc.reason == "missing",
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
