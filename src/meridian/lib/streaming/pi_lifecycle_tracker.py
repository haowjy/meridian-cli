"""Validation and deduplication for produced Pi lifecycle events."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.harness.connections.base import HarnessEvent

logger = logging.getLogger(__name__)


def _is_pi_lifecycle_namespace_label(label: str) -> bool:
    normalized = label.strip().lower()
    return normalized.startswith(("meridian.quiescence.", "meridian_quiescence_"))


@dataclass
class PiLifecycleTracker:
    """Reject malformed produced lifecycle events and deduplicate canonical rows."""

    canonical_event_keys: set[tuple[str, str]]
    lifecycle_tracking_invalidated_error: str | None = None

    @classmethod
    def empty(cls) -> PiLifecycleTracker:
        return cls(canonical_event_keys=set())

    def observe(self, event: HarnessEvent) -> bool:
        """Observe one event and report whether it duplicates a canonical row."""
        if event.harness_id != HarnessId.PI.value:
            return False

        labels = set(pi_lifecycle.pi_lifecycle_event_label_candidates(event))
        lifecycle_schema_error = pi_lifecycle.unsupported_pi_schema_version_error(
            labels, event.payload
        )
        if lifecycle_schema_error is not None:
            self.lifecycle_tracking_invalidated_error = lifecycle_schema_error
            return False
        if self._is_parse_error_for_canonical_lifecycle_event(event):
            raw_type = event.payload.get("raw_type")
            raw_type_text = raw_type if isinstance(raw_type, str) else "unknown"
            self.lifecycle_tracking_invalidated_error = (
                "pi_lifecycle_tracking_invalidated:unsupported_schema_event:"
                f"{raw_type_text}"
            )
            return False

        canonical_labels = sorted(
            label for label in labels if label in pi_lifecycle.PI_LIFECYCLE_EVENT_ALLOWLIST
        )
        if canonical_labels:
            correlation_id = event.payload.get("correlation_id")
            if isinstance(correlation_id, str) and correlation_id.strip():
                key = (canonical_labels[0], correlation_id.strip())
                if key in self.canonical_event_keys:
                    logger.debug(
                        "Ignoring duplicate canonical Pi lifecycle event",
                        extra={"event_type": key[0], "correlation_id": key[1]},
                    )
                    return True
                self.canonical_event_keys.add(key)

        unknown_label = next(
            (
                label
                for label in sorted(labels)
                if label not in pi_lifecycle.PI_LIFECYCLE_EVENT_ALLOWLIST
                and _is_pi_lifecycle_namespace_label(label)
            ),
            None,
        )
        if unknown_label is not None:
            self.lifecycle_tracking_invalidated_error = (
                "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
                f"{unknown_label}"
            )
        return False

    def _is_parse_error_for_canonical_lifecycle_event(self, event: HarnessEvent) -> bool:
        labels = set(pi_lifecycle.pi_lifecycle_event_label_candidates(event))
        if "meridian.lifecycle.parse_error" not in labels:
            return False
        raw_type = event.payload.get("raw_type")
        if not isinstance(raw_type, str) or not raw_type.startswith(
            pi_lifecycle.PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES
        ):
            return False
        return event.payload.get("error") == "unsupported_schema_version"


__all__ = ["PiLifecycleTracker"]
