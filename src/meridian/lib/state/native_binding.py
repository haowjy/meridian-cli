"""Pure native-key binding decisions and write-boundary conflict reporting."""

from dataclasses import dataclass
from typing import Literal

import structlog

from meridian.lib.core.native_identity import BindSource, NativeKeyFields


@dataclass(frozen=True)
class Bound:
    key: NativeKeyFields


@dataclass(frozen=True)
class Same:
    key: NativeKeyFields


@dataclass(frozen=True)
class Conflict:
    kept: NativeKeyFields
    attempted: NativeKeyFields
    field: Literal["harness", "native_store", "session_id", "generation"]


type BindOutcome = Bound | Same | Conflict


def bind(prior: NativeKeyFields, attempted: NativeKeyFields) -> BindOutcome:
    """Fill absent fields; never replace a nonempty field. No I/O."""
    if prior.session_id and attempted.session_id and prior.session_id != attempted.session_id:
        return Conflict(prior, attempted, "session_id")
    if (
        prior.native_store
        and attempted.native_store
        and prior.native_store != attempted.native_store
    ):
        return Conflict(prior, attempted, "native_store")
    if prior.harness and attempted.harness and prior.harness != attempted.harness:
        return Conflict(prior, attempted, "harness")
    merged = NativeKeyFields(
        prior.harness or attempted.harness,
        prior.native_store or attempted.native_store,
        prior.session_id or attempted.session_id,
    )
    return Same(merged) if merged == prior else Bound(merged)


def report_conflict(
    chat_id: str,
    conflict: Conflict,
    source: BindSource | Literal["start"],
) -> None:
    """Report a rejected write once; replay must never call this."""
    structlog.get_logger(__name__).warning(
        "native_binding_conflict",
        chat_id=chat_id,
        kept=conflict.kept.render(),
        attempted=conflict.attempted.render(),
        field=conflict.field,
        source=source,
    )
