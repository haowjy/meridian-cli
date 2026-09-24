"""Pi tree projection tests use isolated synthetic JSONL only."""

from __future__ import annotations

import json
from collections.abc import Mapping

from meridian.lib.harness.pi_journal import project_pi_reopen_default
from meridian.lib.harness.transcript import parse_transcript_events_with_prologues


def _journal(*rows: Mapping[str, object]) -> str:
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def _header() -> dict[str, object]:
    return {"type": "session", "id": "synthetic", "version": 3, "cwd": "/repo"}


def test_projects_reopen_default_lineage_before_common_normalization() -> None:
    source = _journal(
        _header(),
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": "root"},
        },
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {"role": "assistant", "content": "branch B"},
        },
        {
            "type": "message",
            "id": "wrong",
            "parentId": "a",
            "message": {"role": "assistant", "content": "off-lineage wrong"},
        },
        {
            "type": "compaction",
            "id": "compact",
            "parentId": "b",
            "summary": "kept summary",
            "firstKeptEntryId": "b",
        },
        {
            "type": "branch_summary",
            "id": "summary",
            "parentId": "compact",
            "fromId": "wrong",
            "summary": "branch context",
        },
        {
            "type": "message",
            "id": "leaf",
            "parentId": "summary",
            "message": {"role": "assistant", "content": "selected response"},
        },
    )
    projection = project_pi_reopen_default(source)

    assert projection.view_basis == "reopen-default"
    assert projection.complete is True
    assert [event.get("id") for event in projection.events] == [
        "synthetic",
        "a",
        "b",
        "compact",
        "summary",
        "leaf",
    ]
    parsed = parse_transcript_events_with_prologues(projection.events)
    rendered = "\n".join(message.content for segment in parsed.segments for message in segment)
    assert "selected response" in rendered
    assert "off-lineage wrong" not in rendered
    assert parsed.total_compactions == 1
    assert parsed.segment_setups == (None, "kept summary")
    assert any(
        "branch context" in message.content for segment in parsed.segments for message in segment
    )


def test_reports_missing_parent_and_cycle_without_claiming_complete() -> None:
    missing = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "message",
                "id": "a",
                "parentId": "absent",
                "message": {"role": "assistant", "content": "x"},
            },
        )
    )
    cycle = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "message",
                "id": "a",
                "parentId": "b",
                "message": {"role": "assistant", "content": "a"},
            },
            {
                "type": "message",
                "id": "b",
                "parentId": "a",
                "message": {"role": "assistant", "content": "b"},
            },
        )
    )
    assert missing.complete is False
    assert any("missing parent" in reason for reason in missing.reasons)
    assert cycle.complete is False
    assert "cycle in Pi parent chain" in cycle.reasons


def test_torn_tail_is_excluded_and_stable_source_order_is_retained() -> None:
    complete_rows = [
        _header(),
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": "a"},
        },
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {"role": "assistant", "content": "b"},
        },
    ]
    source = _journal(*complete_rows) + '{"type":"message","id":"half"'
    projection = project_pi_reopen_default(source)
    assert projection.complete is False
    assert "torn partial line" in projection.reasons
    assert [event.get("id") for event in projection.events] == ["synthetic", "a", "b"]

    reordered = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {"role": "user", "content": "a"},
            },
            {
                "type": "message",
                "id": "sibling",
                "parentId": "a",
                "message": {"role": "assistant", "content": "sibling"},
            },
            {
                "type": "message",
                "id": "b",
                "parentId": "a",
                "message": {"role": "assistant", "content": "b"},
            },
        )
    )
    assert [event.get("id") for event in reordered.events] == ["synthetic", "a", "b"]


def test_unknown_row_type_marks_projection_incomplete() -> None:
    projection = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "future_pi_entry",
                "id": "unknown",
                "parentId": None,
                "timestamp": "synthetic",
            },
        )
    )
    assert projection.complete is False
    assert "unknown row type: future_pi_entry" in projection.reasons
