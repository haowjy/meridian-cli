"""Published lifecycle event IDs keep their historical namespace."""

import uuid

from meridian.lib.core.lifecycle import generate_event_id


def test_generate_event_id_preserves_legacy_spawn_namespace() -> None:
    expected = uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        "meridian:spawn:p1:spawn.created:0",
    )
    assert generate_event_id("p1", "spawn.created", 0) == expected
