"""Immutable harness-native identity values and typed refusals."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal

Operation = Literal["create", "resume", "fork"]
BindSource = Literal["assigned", "observed", "legacy_import", "legacy_pi_recovery", "user_repair"]
UnavailableReason = Literal["unbound", "missing", "ambiguous_native_file"]
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
    }
    _MESSAGES: ClassVar[dict[UnavailableReason, str]] = {
        "unbound": "no verified native session for {ref}",
        "missing": "native transcript missing or pending for {ref}",
        "ambiguous_native_file": "ambiguous native transcript for {ref}",
    }

    def __init__(self, ref: str, reason: UnavailableReason) -> None:
        self.ref = ref
        self.reason: UnavailableReason = reason
        self.failure_code = self._CODES[reason]
        super().__init__(f"{self.failure_code}: {self._MESSAGES[reason].format(ref=ref)}")

    def for_ref(self, ref: str) -> "NativeSessionUnavailable":
        return NativeSessionUnavailable(ref, self.reason)

    def lifecycle_fields(self) -> dict[str, object]:
        return {"ref": self.ref, "reason": self.reason}


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

    def entry_fields(self) -> NativeKeyFields:
        return NativeKeyFields(self.harness, self.native_store, self.session_id)


@dataclass(frozen=True)
class PostExit:
    """Adapter observations; the runner decides and persists the outcome."""

    entry_error: NativeIdentityError | None = None
    entry_observed: NativeKey | None = None
    exit: NativeKey | None = None
    trampoline_successor_id: str | None = None
