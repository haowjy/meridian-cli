"""Immutable harness-native identity values and typed refusals."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal

Operation = Literal["create", "resume", "fork"]
BindSource = Literal["assigned", "observed", "legacy_import", "legacy_pi_recovery", "user_repair"]
UnavailableReason = Literal["unbound", "missing", "ambiguous_native_file", "store_moved"]
# Why a resume's store differs from the recorded one, as the harness adapter reads it:
# the project directory moved, or the harness's config root changed.
StoreMoveKind = Literal["project_moved", "config_root"]
MismatchReason = Literal["key", "fork_reused_source", "source_changed", "fork_parent"]


@dataclass(frozen=True)
class NativeKey:
    """A complete binding required for exact native reads."""

    harness: str
    native_store: str
    session_id: str

    def fields(self) -> "NativeKeyFields":
        return NativeKeyFields(self.harness, self.native_store, self.session_id)


@dataclass(frozen=True)
class NativeObservation:
    """One launch-owned native identity fact, in harness emission order."""

    key: NativeKey
    entry_operation: Operation | None
    next_cursor: int


@dataclass(frozen=True)
class NativeObservationBatch:
    """Ordered facts with per-fact acknowledgements and a final consumed cursor."""

    observations: tuple[NativeObservation, ...] = ()
    next_cursor: int = 0


@dataclass(frozen=True)
class NativeKeyFields:
    """A partial binding, including legacy and pre-observation records."""

    harness: str | None = None
    native_store: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness", self.harness or None)
        object.__setattr__(self, "native_store", self.native_store or None)
        object.__setattr__(self, "session_id", self.session_id or None)

    def complete(self) -> NativeKey | None:
        if self.harness is None or self.native_store is None or self.session_id is None:
            return None
        return NativeKey(self.harness, self.native_store, self.session_id)

    def with_session(self, session_id: str) -> "NativeKeyFields":
        return replace(self, session_id=session_id)

    def render(self) -> dict[str, str | None]:
        return {
            "harness": self.harness,
            "native_store": self.native_store,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class NativeStoreMove:
    """A resume would run in ``attempted`` while the chat is recorded in ``recorded``.

    ``fix`` is the adapter's line for a ``config_root`` difference: how the user makes
    the resume find the recorded store without moving the chat. ``also_moved`` says the
    project directory moved as well, so that fix alone is not enough.
    """

    recorded: str
    attempted: str
    kind: StoreMoveKind
    fix: str | None = None
    also_moved: bool = False

    def render(self) -> dict[str, str]:
        return {"recorded": self.recorded, "attempted": self.attempted, "kind": self.kind}


class NativeIdentityError(ValueError):
    """Base for every typed identity refusal."""

    failure_code: str

    def lifecycle_fields(self) -> dict[str, object]:
        raise NotImplementedError


class NativeSessionUnavailable(NativeIdentityError):
    """A reference has no exact readable native target."""

    _CODES: ClassVar[dict[UnavailableReason, str]] = {
        "unbound": "unbound",
        "missing": "native_transcript_missing",
        "ambiguous_native_file": "ambiguous_native_file",
        "store_moved": "native_store_moved",
    }
    _MESSAGES: ClassVar[dict[UnavailableReason, str]] = {
        "unbound": "no verified native session for {ref}",
        "missing": "native transcript missing or pending for {ref}",
        "ambiguous_native_file": "ambiguous native transcript for {ref}",
        "store_moved": "{ref} is recorded in native store {recorded}, but this launch would use "
        "{attempted} ({kind})",
    }

    def __init__(
        self,
        ref: str,
        reason: UnavailableReason,
        *,
        store_move: NativeStoreMove | None = None,
        hint: str | None = None,
    ) -> None:
        self.ref = ref
        self.reason: UnavailableReason = reason
        self.store_move = store_move
        self.hint = hint
        self.failure_code = self._CODES[reason]
        details = store_move.render() if store_move is not None else {}
        message = f"{self.failure_code}: {self._MESSAGES[reason].format(ref=ref, **details)}"
        super().__init__(f"{message}\n{hint}" if hint else message)

    def for_ref(self, ref: str) -> "NativeSessionUnavailable":
        return NativeSessionUnavailable(
            ref, self.reason, store_move=self.store_move, hint=self.hint
        )

    def with_hint(self, hint: str | None) -> "NativeSessionUnavailable":
        """This refusal with ``hint`` added after the hint it already carries."""
        joined = "\n".join(part for part in (self.hint, hint) if part) or None
        return NativeSessionUnavailable(
            self.ref, self.reason, store_move=self.store_move, hint=joined
        )

    def lifecycle_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {"ref": self.ref, "reason": self.reason}
        if self.store_move is not None:
            fields["store_move"] = self.store_move.render()
        return fields


class NativeEntryMismatch(NativeIdentityError):
    """An owned initial identity contradicts the exact launch target."""

    failure_code = "entry_mismatch"

    def __init__(
        self,
        expected: NativeKeyFields,
        observed: NativeKeyFields,
        reason: MismatchReason = "key",
        detail: str | None = None,
    ) -> None:
        self.expected = expected
        self.observed = observed
        self.reason: MismatchReason = reason
        self.detail = detail
        super().__init__(
            f"entry_mismatch: expected {expected.render()}, observed {observed.render()}"
            f" (reason={reason})" + (f": {detail}" if detail is not None else "")
        )

    def lifecycle_fields(self) -> dict[str, object]:
        return {
            "expected": self.expected.render(),
            "observed": self.observed.render(),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LaunchIntent:
    """Identity request before the child's native store is known."""

    operation: Operation
    source_session_id: str | None = None
    preforked_session_id: str | None = None


@dataclass(frozen=True)
class NativeIdentity:
    """Finalized entry identity selected before exec."""

    harness: str
    operation: Operation
    native_store: str
    session_id: str | None
    source_session_id: str | None
    source: Path | None
    # The file the miss-path search found when the recorded one was missing; equals
    # ``source``. The prelaunch links it back into ``native_store``.
    located_from: Path | None = None

    def entry_fields(self) -> NativeKeyFields:
        return NativeKeyFields(self.harness, self.native_store, self.session_id)


@dataclass(frozen=True)
class PostExit:
    """Adapter observations; the runner decides and persists the outcome."""

    entry_error: NativeIdentityError | None = None
    entry_observed: NativeKey | None = None
    exit: NativeKey | None = None
    trampoline_successor_id: str | None = None
