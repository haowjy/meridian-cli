"""Effective native-session selection checks shared by launch seams."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

_CHAT_REF = re.compile(r"c[1-9][0-9]*\Z")
_SPAWN_REF = re.compile(r"p[1-9][0-9]*\Z")


def normalize_effective_native_selection(
    source_ref: str | None,
    native_selector: str | None,
    *,
    authorized_native_id: str | None = None,
    tracked_claim: bool = False,
) -> str | None:
    """Normalize source intent and executable selector without losing either.

    cN/pN are Meridian aliases and may differ from the executable native ID;
    callers resolving one must pass its authorized native ID. Bare native refs
    must exactly match the native selector. A tracked boolean never classifies
    a selector as untracked.
    """
    source = (source_ref or "").strip() or None
    selector = (native_selector or "").strip() or None
    authorized = (authorized_native_id or "").strip() or None

    if source_ref is not None and source is None and selector is not None:
        raise ValueError(
            "Original source reference does not match the executable native "
            "selection; source-use authorization refused."
        )
    if tracked_claim and source is None:
        raise ValueError(
            "Cannot use tracked source without its original reference: "
            "source-use authorization refused."
        )
    if source is not None and selector is not None:
        if _CHAT_REF.fullmatch(source) or _SPAWN_REF.fullmatch(source):
            if authorized is not None and selector != authorized:
                raise ValueError(
                    "Original source reference does not match the authorized "
                    "native selection; source-use authorization refused."
                )
        elif source != selector:
            raise ValueError(
                "Original source reference does not match the executable native "
                "selection; source-use authorization refused."
            )
    if authorized is not None and selector is not None and selector != authorized:
        raise ValueError(
            "Executable native selection does not match the authorized source; "
            "source-use authorization refused."
        )
    return source or selector


def validate_primary_source_use(
    *,
    runtime_root: Path,
    source_ref: str | None,
    native_selector: str | None,
    tracked_claim: bool,
    recorded_source: object | None,
    harness: str | None,
    operation: Literal["resume", "fork"],
    extra_args: tuple[str, ...] = (),
) -> object | None:
    """Strictly validate the actual source selection and retain its negative result locally."""
    from meridian.lib.ops.reference import (
        AuthorizedSourceUse,
        SourceUseRefused,
        UntrackedSourceUse,
        resolve_source_use,
    )

    normalized = normalize_effective_native_selection(
        source_ref, native_selector, tracked_claim=tracked_claim
    )
    if (harness or "").strip().lower() == "pi":
        from meridian.lib.harness.pi_native_source import reject_pi_native_source_options

        reject_pi_native_source_options(extra_args)
    result = (
        resolve_source_use(runtime_root, operation, normalized, harness)
        if normalized
        else None
    )
    if normalized:
        if isinstance(result, AuthorizedSourceUse):
            normalize_effective_native_selection(
                source_ref,
                native_selector,
                authorized_native_id=result.source.key.native_session_id,
                tracked_claim=tracked_claim,
            )
            raise ValueError(
                f"Cannot {operation} tracked source on the primary launch transport: "
                "transport_unqualified. Tracked primary resume/fork is unsupported "
                "until an owned transport is available."
            )
        if isinstance(result, UntrackedSourceUse):
            normalize_effective_native_selection(
                source_ref,
                native_selector,
                authorized_native_id=result.native_id,
                tracked_claim=tracked_claim,
            )
        if isinstance(result, SourceUseRefused):
            raise ValueError(
                f"Cannot use source '{normalized}': source-use authorization "
                f"refused ({result.reason})."
            )
    if recorded_source is not None:
        raise ValueError(
            "Cannot launch a caller-supplied tracked source without revalidated "
            "source-use provenance (transport_unqualified)."
        )
    return result
