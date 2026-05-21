from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.telemetry.retention import SegmentOwner, parse_segment_owner


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("cli.123-0001.jsonl", SegmentOwner(logical_owner="cli", pid=123)),
        (
            "spawn.worker.alpha.321-0003.jsonl",
            SegmentOwner(logical_owner="spawn.worker.alpha", pid=321),
        ),
    ],
)
def test_parse_segment_owner_accepts_compound_segment_names(
    name: str,
    expected: SegmentOwner,
) -> None:
    assert parse_segment_owner(Path(name)) == expected


@pytest.mark.parametrize(
    "name",
    [
        "123-0001.jsonl",
        "cli.123.jsonl",
        "cli.-0001.jsonl",
        "cli.pid-0001.jsonl",
        "cli.123-seq.jsonl",
        "cli.123-0001.log",
    ],
)
def test_parse_segment_owner_rejects_legacy_and_malformed_names(name: str) -> None:
    assert parse_segment_owner(Path(name)) is None
