"""Request-id extraction from OpenCode permission ask payloads."""

from __future__ import annotations

import pytest

from meridian.lib.harness.connections.opencode_http import (
    _extract_opencode_permission_context,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        # A bare event id is the last resort only.
        ({"id": "evt_1"}, (None, "evt_1")),
        # An explicit request-id key outranks a bare id at any nesting level.
        ({"requestID": "req_1", "id": "evt_1"}, (None, "req_1")),
        (
            {"requestID": "req_1", "properties": {"id": "evt_1"}},
            (None, "req_1"),
        ),
        # A per_… value outranks both explicit and generic ids.
        (
            {"requestID": "req_1", "id": "evt_1", "properties": {"id": "per_9"}},
            (None, "per_9"),
        ),
        # Session id comes from the same envelope or properties.
        (
            {"requestID": "req_1", "properties": {"sessionID": "ses_1"}},
            ("ses_1", "req_1"),
        ),
        # V1 shape: the per_ id lives under the generic id key.
        (
            {"id": "per_1", "sessionID": "ses_1"},
            ("ses_1", "per_1"),
        ),
        ({}, (None, None)),
    ),
)
def test_extract_opencode_permission_context_prefers_specific_ids(
    payload: dict[str, object],
    expected: tuple[str | None, str | None],
) -> None:
    assert _extract_opencode_permission_context(payload) == expected
