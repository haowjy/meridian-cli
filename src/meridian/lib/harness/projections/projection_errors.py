from __future__ import annotations


class HarnessCapabilityMismatch(ValueError):
    """Raised when requested launch semantics cannot be represented on a harness."""


__all__ = ["HarnessCapabilityMismatch"]
