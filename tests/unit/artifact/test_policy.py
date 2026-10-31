"""Artifact policy: TTL/expiry, port selection and slug derivation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meridian.lib.artifact import service
from meridian.lib.artifact.store import Serve


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("2h", 7200), ("30m", 1800), ("90s", 90), ("1d", 86400)],
)
def test_parse_ttl(value: str, seconds: int) -> None:
    assert service.parse_ttl(value) == seconds


@pytest.mark.parametrize("value", ["", "2", "1w", "0h", "-1m", "1.5h"])
def test_parse_ttl_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        service.parse_ttl(value)


def test_expiry_is_strict_and_persistent_never_expires() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    base = Serve("report", 50000, "/tmp", None, "2026-01-01T00:00:00Z", 60, False, 123)
    persistent = Serve("keep", 50001, "/tmp", None, base.created, None, False, 124)

    assert not service.is_expired(base, created + timedelta(seconds=60))
    assert service.is_expired(base, created + timedelta(seconds=61))
    assert not service.is_expired(persistent, created + timedelta(days=365))


def test_pick_port_skips_occupied_and_failed_bind_probe() -> None:
    candidates = iter([50000, 50001, 50002])

    port = service.pick_port(
        {50000},
        lambda candidate: candidate != 50001,
        choose=lambda _low, _high: next(candidates),
    )

    assert port == 50002


def test_slug_derivation_sanitizes_and_resolves_collisions() -> None:
    assert service.derive_slug(Path("/tmp/My  Report!!"), None, set(), 50000) == "my-report"
    assert service.derive_slug(Path("/tmp/report"), None, {"report"}, 51234) == "report-51234"
    assert (
        service.derive_slug(Path("/tmp/report"), None, {"report", "report-51234"}, 51234)
        == "report-51234-2"
    )
    with pytest.raises(service.ArtifactError):
        service.derive_slug(Path("/"), "/", set(), 50000)
