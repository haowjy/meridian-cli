"""``meridian session relocate``: the one way a chat's recorded paths change.

Two questions about a chat whose directory may have moved, deliberately kept apart:
``needs_relocate`` asks whether its next launch would fail because of a moved
directory; ``moved_root`` asks where it moved from. One rule for both made the gate
fire for chats that resume fine.
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
    harness resumes in is gone."""

    store_move: NativeStoreMove | None = None
    missing_dir: Path | None = None


def needs_relocate(
    generations: Sequence[SessionRecord],
    adapter: SubprocessHarness,
    *,
    control_root: Path,
    task_dir: Path | None,
    child_env: Mapping[str, str],
) -> RelocateNeed | None:
    """The gate: would resuming this chat from ``control_root`` fail on a moved path?

    ``generations`` are the chat's (``session_store.chat_generations``); ``task_dir`` is
    the directory Meridian would resume in, the continue contract's. A store the launch
    can't pin (``unpinnable``) is not a move: the launch reports it as unavailable.
    """
    newest_first = tuple(reversed(generations))
    identity = _chat_key(newest_first)
    if identity.native_store is not None:
        attempted = adapter.resume_store(
            identity.native_store, child_env=child_env, child_cwd=control_root
        )
        if attempted != identity.native_store:
            kind = adapter.classify_store_move(recorded=identity.native_store, attempted=attempted)
            if kind != "unpinnable":
                return RelocateNeed(
                    store_move=NativeStoreMove(identity.native_store, attempted, kind)
                )
    # A harness without its own record of the directory resumes where Meridian does.
    resumes_in = adapter.native_cwd(identity, recorded_roots=_control_roots(newest_first))
    resumes_in = resumes_in or task_dir
    if resumes_in is not None and not resumes_in.is_dir():
        return RelocateNeed(missing_dir=resumes_in)
    return None


def moved_root(generations: Sequence[SessionRecord], adapter: SubprocessHarness) -> Path | None:
    """The directory this chat moved from: relocate's default ``--from``.

    The harness's own record wins. The chat record's ``control_root`` is never used: a
    ``--continue`` from the new directory by an older build appended a start there.
    """
    newest_first = tuple(reversed(generations))
    recorded = adapter.native_cwd(
        _chat_key(newest_first), recorded_roots=_control_roots(newest_first)
    )
    if recorded is not None:
        return recorded
    for generation in newest_first:
        # Legacy rows carry no control_root.
        root = generation.control_root or generation.task_cwd or generation.execution_cwd
        if root:
            return Path(root)
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
