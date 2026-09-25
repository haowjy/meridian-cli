"""Immutable harness-native launch identity."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NativeIdentityPlan:
    """Exact native identity selected before exec; forks may await an owned signal."""

    harness_session_id: str | None
    native_store: str | None
    locator: str | None
    operation: Literal["create", "resume", "fork"]


class NativeSessionUnavailable(ValueError):
    """A tracked reference has no exact readable native target."""

    def __init__(
        self, ref: str, reason: Literal["unbound", "missing", "ambiguous_native_file"]
    ) -> None:
        self.ref = ref
        self.reason = reason
        self.failure_code = "native_transcript_missing" if reason == "missing" else reason
        message = (
            f"no verified native session for {ref}"
            if reason == "unbound"
            else f"ambiguous native transcript for {ref}"
            if reason == "ambiguous_native_file"
            else f"native transcript missing or pending for {ref}"
        )
        super().__init__(f"{self.failure_code}: {message}")
