"""Fail-closed normalization for serialized spawn native selections."""

from pathlib import Path

from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.reference import (
    AuthorizedSourceUse,
    SourceUseRefused,
    UntrackedSourceUse,
    resolve_source_use,
)


def normalize_untracked_spawn_selection(
    session: SessionRequest,
    *,
    runtime_root: Path,
    harness: str | None,
) -> SessionRequest:
    """Validate all selection spellings and return their single effective ID.

    The source reference and executable native ID are independent serialized
    fields, so both must resolve to the same claim. Tracked launches remain
    blocked until an owned starter exists; this check is repeated in the worker.
    """

    native_id = (session.requested_harness_session_id or "").strip() or None
    source_ref = (session.continue_source_ref or "").strip() or None
    credential = session.recorded_native_source
    has_intent = bool(native_id or source_ref or credential or session.continue_source_tracked)
    if not has_intent:
        return session

    operation = "fork" if session.continue_fork else "resume"
    refs = tuple(dict.fromkeys(ref for ref in (source_ref, native_id) if ref))
    if not refs:
        raise ValueError(
            "Tracked spawn source intent has no native selection; no process was started."
        )

    results = [
        resolve_source_use(
            runtime_root,
            operation,
            ref,
            explicit_harness=(session.continue_harness or harness),
        )
        for ref in refs
    ]
    if any(isinstance(result, SourceUseRefused) for result in results):
        refused = next(result for result in results if isinstance(result, SourceUseRefused))
        raise ValueError(
            f"Spawn source '{refused.original_ref}' is unavailable "
            f"({refused.reason}); no process was started."
        )

    first = results[0]
    for result in results[1:]:
        if isinstance(first, AuthorizedSourceUse) and isinstance(result, AuthorizedSourceUse):
            agrees = first.source.key == result.source.key and first.source.ref == result.source.ref
        elif isinstance(first, UntrackedSourceUse) and isinstance(result, UntrackedSourceUse):
            agrees = first.native_id == result.native_id
        else:
            agrees = False
        if not agrees:
            raise ValueError(
                "Spawn source reference and native selection disagree; no process was started."
            )

    if isinstance(first, AuthorizedSourceUse):
        if credential is not None and (
            credential.key != first.source.key or credential.ref != first.source.ref
        ):
            raise ValueError(
                "Recorded spawn source does not match current authority; no process was started."
            )
        raise ValueError(
            "Tracked exact resume is blocked pending owned admission "
            "(owner_required); no process was started."
        )

    assert isinstance(first, UntrackedSourceUse)
    if session.continue_source_tracked:
        raise ValueError(
            "Tracked spawn source intent has no recorded native claim; no process was started."
        )
    if credential is not None:
        raise ValueError(
            "Recorded spawn source cannot be treated as untracked; no process was started."
        )
    return session.model_copy(update={
        "requested_harness_session_id": first.native_id,
        "continue_harness": first.harness or session.continue_harness or harness,
    })


__all__ = ["normalize_untracked_spawn_selection"]
