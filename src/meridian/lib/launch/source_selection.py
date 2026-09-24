"""Effective native-session selection checks shared by launch seams."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from meridian.lib.ops.reference import UntrackedSourceUse

_CHAT_REF = re.compile(r"c[1-9][0-9]*\Z")
_SPAWN_REF = re.compile(r"p[1-9][0-9]*\Z")


@dataclass(frozen=True)
class PrimarySourceSelection:
    """Independent source, operation, namespace and harness facts at one owner."""

    source_ref: str | None
    native_id: str | None
    operation: Literal["fresh", "resume", "fork"]
    harness: str | None
    runtime_root: Path
    seed_id: str | None = None
    other_harnesses: tuple[str | None, ...] = ()
    tracked_claim: bool = False
    operation_facts: tuple[str, ...] = ()


def reconcile_primary_source_selection(
    selection: PrimarySourceSelection,
    *,
    authorized_source: UntrackedSourceUse | None = None,
    resolved_id: str | None = None,
    resolved_id_supplied: bool = False,
    resolved_harness: str | None = None,
    resolved_snapshot_harness: str | None = None,
    resolved_tracked: bool = False,
    resolved_source_ref: str | None = None,
    resolved_source_ref_supplied: bool = False,
    resolved_operation: Literal["fresh", "resume", "fork"] | None = None,
    resolved_operation_facts: tuple[str, ...] = (),
) -> str | None:
    """Purely reconcile source facts without choosing a winning selector.

    ``authorized_source`` and resolver fields are values retained by the
    current synchronous owner, not transferable permission. Alias references
    (cN/pN) are compared to a native ID only after a local native value exists.
    """
    source_supplied = selection.source_ref is not None
    source = _optional(selection.source_ref)
    native_id = _optional(selection.native_id)
    seed_id = _optional(selection.seed_id)
    harness = _optional(selection.harness)
    authorized_id = _optional(authorized_source.native_id) if authorized_source else None
    authorized_harness = _optional(authorized_source.harness) if authorized_source else None
    resolved_native = _optional(resolved_id)
    resolved_harness_id = _optional(resolved_harness)
    resolved_snapshot_harness_id = _optional(resolved_snapshot_harness)
    resolved_source = _optional(resolved_source_ref)

    if resolved_id_supplied and resolved_native is None:
        raise _conflict("resolver dropped the selected native source")
    if selection.tracked_claim:
        raise _conflict("caller claimed tracked source without a native admission")
    if selection.operation_facts and any(
        fact != selection.operation_facts[0] for fact in selection.operation_facts[1:]
    ):
        raise _conflict("conflicting operation facts")
    if selection.operation_facts and selection.operation_facts[0] != selection.operation:
        raise _conflict("operation differs from supplied operation facts")

    if source_supplied and source is None and any((native_id, seed_id)):
        raise _conflict("blank original source reference")
    if selection.operation == "fresh" and any((source, native_id, seed_id, resolved_native)):
        raise _conflict("fresh operation has a continuation source")
    if selection.operation in ("resume", "fork") and source is None and native_id is None:
        raise _conflict("continuation operation has no original source")

    native_facts = [item for item in (native_id, seed_id, authorized_id, resolved_native) if item]
    if native_facts and any(value != native_facts[0] for value in native_facts[1:]):
        raise _conflict("native source selection changed")
    for native_fact in native_facts:
        normalize_effective_native_selection(selection.source_ref, native_fact)
    if resolved_tracked:
        raise _conflict("legacy resolver classified source as tracked")
    if resolved_source_ref_supplied and resolved_source != source:
        raise _conflict("original source reference changed")
    operation_facts = resolved_operation_facts
    if resolved_operation is not None:
        operation_facts = (*operation_facts, resolved_operation)
    if operation_facts and any(fact != operation_facts[0] for fact in operation_facts[1:]):
        raise _conflict("conflicting resolved operation facts")
    if operation_facts and operation_facts[0] != selection.operation:
        raise _conflict("source operation changed")

    harness_facts = [
        item
        for item in (
            harness,
            *selection.other_harnesses,
            authorized_harness,
            resolved_harness_id,
            resolved_snapshot_harness_id,
        )
        if item
    ]
    if harness_facts and any(
        value.lower() != harness_facts[0].lower() for value in harness_facts[1:]
    ):
        raise _conflict("source harness changed")

    if authorized_source is not None:
        if authorized_source.operation != selection.operation:
            raise _conflict("source operation changed")
        if source and authorized_source.original_ref.strip() != source:
            raise _conflict("authorized original reference changed")
        if authorized_source.lookup_scope != selection.runtime_root:
            raise _conflict("source lookup namespace changed")
    return native_facts[0] if native_facts else None


def _optional(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def _is_alias(source: str) -> bool:
    # Resolvers also accept older opaque chat IDs (for example ``c-spawn``);
    # only an unprefixed source is treated as a bare native selector here.
    return (
        _CHAT_REF.fullmatch(source) is not None
        or _SPAWN_REF.fullmatch(source) is not None
        or source.startswith(("c-", "p-"))
    )


def _conflict(detail: str) -> ValueError:
    return ValueError(
        f"Primary source selection conflict ({detail}); source-use authorization refused."
    )


def normalize_effective_native_selection(
    source_ref: str | None,
    native_selector: str | None,
    *,
    authorized_native_id: str | None = None,
    tracked_claim: bool = False,
) -> str | None:
    """Normalize source intent and executable selector without losing either.

    cN/pN are Meridian aliases and may differ from the executable native ID;
    callers resolving one must pass its authorized native ID. A tracked boolean
    never classifies a selector as untracked.
    """
    source = _optional(source_ref)
    selector = _optional(native_selector)
    authorized = _optional(authorized_native_id)

    if source_ref is not None and source is None and selector is not None:
        raise _conflict("blank original source reference")
    if tracked_claim and source is None:
        raise ValueError(
            "Cannot use tracked source without its original reference: "
            "source-use authorization refused."
        )
    if source is not None and selector is not None:
        if _is_alias(source):
            if authorized is not None and selector != authorized:
                raise _conflict("original reference differs from authorized native source")
        elif source != selector:
            raise _conflict("original reference differs from native source")
    if authorized is not None and selector is not None and selector != authorized:
        raise _conflict("native source differs from authorized source")
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
) -> UntrackedSourceUse | None:
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
    return result if isinstance(result, UntrackedSourceUse) else None
