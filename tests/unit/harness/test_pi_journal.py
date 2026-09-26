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
            "message": {"role": "assistant", "provider": "p", "model": "m", "content": "branch B"},
        },
        {
            "type": "message",
            "id": "wrong",
            "parentId": "a",
            "message": {
                "role": "assistant",
                "provider": "p",
                "model": "m",
                "content": "off-lineage wrong",
            },
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
            "message": {
                "role": "assistant",
                "provider": "p",
                "model": "m",
                "content": "selected response",
            },
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


def test_linear_pi_journal_keeps_its_existing_normalized_messages() -> None:
    projection = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "message",
                "id": "user",
                "parentId": None,
                "message": {"role": "user", "content": "question"},
            },
            {
                "type": "message",
                "id": "assistant",
                "parentId": "user",
                "message": {
                    "role": "assistant",
                    "provider": "p",
                    "model": "m",
                    "content": "answer",
                },
            },
        )
    )
    parsed = parse_transcript_events_with_prologues(projection.events)

    assert projection.complete is True
    assert projection.view_basis == "reopen-default"
    assert projection.reasons == ()
    messages = [
        (message.role, message.content) for segment in parsed.segments for message in segment
    ]
    assert messages == [
        ("user", "question"),
        ("assistant", "answer"),
    ]


def test_context_edits_and_usage_preserve_pi_chain_and_search_replacement_text() -> None:
    projection = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "message",
                "id": "user",
                "parentId": None,
                "message": {"role": "user", "content": "question"},
            },
            {
                "type": "message",
                "id": "answer",
                "parentId": "user",
                "message": {
                    "role": "assistant",
                    "provider": "p",
                    "model": "m",
                    "content": "original answer",
                },
            },
            {
                "type": "context_edit",
                "id": "remove",
                "parentId": "answer",
                "targetId": "user",
                "replacement": None,
            },
            {
                "type": "usage",
                "id": "usage",
                "parentId": "remove",
                "kind": "cache_warm",
                "provider": "p",
                "model": "m",
                "usage": {},
            },
            {
                "type": "context_edit",
                "id": "replace",
                "parentId": "usage",
                "targetId": "answer",
                "replacement": {"content": [{"type": "text", "text": "replacement needle"}]},
            },
        )
    )

    parsed = parse_transcript_events_with_prologues(projection.events)
    messages = [message for segment in parsed.segments for message in segment]
    rendered = "\n".join(message.content for message in messages)

    assert projection.complete is True
    assert projection.reasons == ()
    assert parsed.rendering_reason is None
    assert "rendering is incomplete" not in rendered
    assert "Pi context edit: removed user from model context" in rendered
    assert "Pi context edit: replaced content of answer" in rendered
    assert "original answer" in rendered
    assert "Pi journal parent changed" not in rendered
    assert not any("usage" in message.content for message in messages)
    replacement_annotation = next(
        message for message in messages if message.content.endswith("replaced content of answer")
    )
    assert replacement_annotation.search_content == "replacement needle"


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
    assert "missing_parent" in missing.reasons
    assert cycle.complete is False
    assert "cycle" in cycle.reasons


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
    assert "malformed_row" in projection.reasons
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
    assert "unknown_type" in projection.reasons


def test_0871_legacy_v3_matches_native_last_entry_and_complete_unterminated_row() -> None:
    source = _journal(
        _header(),
        {
            "type": "message",
            "id": "root",
            "parentId": None,
            "message": {"role": "user", "content": "root"},
        },
        {
            "type": "message",
            "id": "selected",
            "parentId": "root",
            "message": {"role": "assistant", "provider": "p", "model": "m", "content": "selected"},
        },
    ).rstrip("\n")
    projection = project_pi_reopen_default(source)
    assert projection.complete is True
    assert [event.get("id") for event in projection.events] == ["synthetic", "root", "selected"]


def test_leaf_directive_is_unsupported_not_a_selection_dialect() -> None:
    projection = project_pi_reopen_default(
        _journal(
            _header(),
            {"type": "message", "id": "root", "parentId": None},
            {"type": "leaf", "id": "select", "parentId": "wrong", "targetId": "root"},
        )
    )
    assert projection.complete is False
    assert any("unsupported" in reason for reason in projection.reasons)


def test_malformed_leaf_directive_cannot_become_complete_empty_projection() -> None:
    projection = project_pi_reopen_default(
        _journal(
            _header(),
            {"type": "leaf", "id": "select", "parentId": "wrong"},
        )
    )
    assert projection.complete is False
    assert len(projection.events) == 1


def test_selected_ancestry_is_parent_order_not_physical_order() -> None:
    projection = project_pi_reopen_default(
        _journal(
            _header(),
            {
                "type": "message",
                "id": "child",
                "parentId": "root",
                "message": {"role": "assistant", "provider": "p", "model": "m"},
            },
            {"type": "message", "id": "root", "parentId": None, "message": {"role": "user"}},
            {
                "type": "label",
                "id": "leaf",
                "parentId": "child",
                "targetId": "child",
                "label": "branch",
            },
        )
    )
    assert projection.complete is True
    assert [event.get("id") for event in projection.events] == [
        "synthetic",
        "root",
        "child",
        "leaf",
    ]


def test_stream_event_names_are_not_native_journal_entry_types() -> None:
    projection = project_pi_reopen_default(
        _journal(_header(), {"type": "text_delta", "id": "x", "parentId": None})
    )
    assert projection.complete is False
    assert "unknown_type" in projection.reasons
