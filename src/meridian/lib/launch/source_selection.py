"""Effective native-session selection checks shared by launch seams."""

from __future__ import annotations

import re
from collections.abc import Sequence

_CHAT_REF = re.compile(r"c[1-9][0-9]*\Z")
_SPAWN_REF = re.compile(r"p[1-9][0-9]*\Z")


def normalize_effective_native_selection(
    source_ref: str | None,
    native_selector: str | None,
    *,
    authorized_native_id: str | None = None,
    tracked_claim: bool = False,
    extra_args: Sequence[str] = (),
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
    source_flags = {
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--session-id",
        "--session",
        "--fork",
    }
    if any(
        token in source_flags
        or any(token.startswith(f"{flag}=") for flag in source_flags if flag.startswith("--"))
        for token in extra_args
    ):
        raise ValueError(
            "Pi native-session selectors in passthrough arguments are unsupported; "
            "use Meridian source selection so native lineage can be authorized."
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
