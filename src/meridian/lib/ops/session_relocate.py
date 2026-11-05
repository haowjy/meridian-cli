"""``meridian session relocate``: the one way a chat's recorded paths change.

Two questions about a chat whose directory may have moved, deliberately kept apart:
``needs_relocate`` asks whether its next launch would fail because the harness's own
state points at a moved path; ``moved_root`` asks where it moved from. The gate reads
only harness-native reasons, so a relocate that succeeds always clears it. A missing
Meridian task dir (spawn ``--continue``) is not a gate reason: it fails before launch
on every harness and gets a hint built from ``moved_root`` instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.native_identity import NativeKeyFields, NativeStoreMove
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.ops.runtime import async_from_sync
from meridian.lib.state.session_fold import SessionRecord


@dataclass(frozen=True)
class RelocateNeed:
    """Why the next launch of a chat would fail: its store moved, or the directory the
    harness records for it is gone."""

    store_move: NativeStoreMove | None = None
    missing_dir: Path | None = None


def needs_relocate(
    generations: Sequence[SessionRecord],
    adapter: SubprocessHarness,
    *,
    control_root: Path,
    child_env: Mapping[str, str],
) -> RelocateNeed | None:
    """The gate: would resuming this chat from ``control_root`` fail on a moved path?

    ``generations`` are the chat's (``session_store.chat_generations``). The store check
    agrees with the launch's finalize check only when ``child_env`` is that launch's
    final child env.
    """
    newest_first = tuple(reversed(generations))
    identity = _chat_key(newest_first)
    if identity.native_store is not None:
        attempted = adapter.resume_store(
            identity.native_store, child_env=child_env, child_cwd=control_root
        )
        kind = (
            adapter.classify_store_move(recorded=identity.native_store, attempted=attempted)
            if attempted != identity.native_store
            else None
        )
        if kind is not None:
            return RelocateNeed(store_move=NativeStoreMove(identity.native_store, attempted, kind))
    resumes_in = adapter.native_cwd(identity, recorded_roots=_control_roots(newest_first))
    if resumes_in is not None and not resumes_in.is_dir():
        return RelocateNeed(missing_dir=resumes_in)
    return None


def moved_root(
    generations: Sequence[SessionRecord], adapter: SubprocessHarness, *, target: Path
) -> Path | None:
    """The directory this chat moved from to ``target``: relocate's default ``--from``.

    The harness's own record wins, then each generation newest first, by ``task_cwd``,
    ``execution_cwd``, ``control_root``. Any recorded path equal to ``target`` is
    skipped: a ``--continue`` from the new directory, by a build without the gate,
    appended a start that records it.
    """
    newest_first = tuple(reversed(generations))
    roots = tuple(root for root in _control_roots(newest_first) if root != target)
    recorded = adapter.native_cwd(_chat_key(newest_first), recorded_roots=roots)
    if recorded is not None and recorded != target:
        return recorded
    for generation in newest_first:
        # Legacy rows carry no control_root.
        for field in (generation.task_cwd, generation.execution_cwd, generation.control_root):
            if field and Path(field) != target:
                return Path(field)
    return None


def _chat_key(newest_first: Sequence[SessionRecord]) -> NativeKeyFields:
    for generation in newest_first:
        if generation.native_key() is not None:
            return generation.key_fields()
    return newest_first[0].key_fields() if newest_first else NativeKeyFields()


def _control_roots(newest_first: Sequence[SessionRecord]) -> tuple[Path, ...]:
    return tuple(Path(g.control_root) for g in newest_first if g.control_root)


class SessionRelocateInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str | None = None
    from_root: str | None = None
    to_root: str | None = None
    native: str | None = None
    dry_run: bool = False
    project_root: str | None = None


class SessionRelocateOutput(BaseModel):
    model_config = ConfigDict(frozen=True)


def session_relocate_sync(payload: SessionRelocateInput) -> SessionRelocateOutput:
    _ = payload
    raise ValueError("meridian session relocate is not implemented yet.")


session_relocate = async_from_sync(session_relocate_sync)
