from __future__ import annotations

import pytest

from meridian.cli.browse.model import build_lineage_nodes
from meridian.cli.session_cmd import (
    Interactive,
    Plain,
    Refused,
    resolve_browse_presentation,
)
from meridian.lib.ops.session_lineage import SessionLineageEdge


def test_lineage_tree_draws_multi_parent_node_under_each_parent_and_survives_cycles() -> None:
    edges = (
        SessionLineageEdge(child_chat_id="c2", parent_chat_id="c1", kind="fork"),
        SessionLineageEdge(child_chat_id="c3", parent_chat_id="c1", kind="from"),
        SessionLineageEdge(child_chat_id="c3", parent_chat_id="c2", kind="fork"),
        SessionLineageEdge(child_chat_id="c1", parent_chat_id="c3", kind="from"),
    )
    nodes = build_lineage_nodes("c2", edges, {})
    assert [node.chat_id for node in nodes].count("c3") >= 1
    assert len(nodes) < 20  # the c1 -> c3 -> c1 loop must not recurse forever

    tree = build_lineage_nodes(
        "c3",
        (
            SessionLineageEdge(child_chat_id="c2", parent_chat_id="c1", kind="fork"),
            SessionLineageEdge(child_chat_id="c3", parent_chat_id="c1", kind="from"),
            SessionLineageEdge(child_chat_id="c3", parent_chat_id="c2", kind="fork"),
        ),
        {},
    )
    assert [(n.chat_id, n.kind) for n in tree] == [
        ("c1", ""),
        ("c2", "fork"),
        ("c3", "fork"),
        ("c3", "from"),
    ]


@pytest.mark.parametrize(
    ("kwargs", "expected_type"),
    [
        ({"plain": True}, Plain),
        ({"stdin_tty": False}, Plain),
        ({"stdout_tty": False}, Plain),
        ({"term": "dumb"}, Plain),
        ({"term": None}, Plain),
        ({"managed": True}, Refused),
        ({}, Interactive),
    ],
)
def test_resolve_browse_presentation(kwargs: dict[str, object], expected_type: type) -> None:
    inputs: dict[str, object] = {
        "plain": False,
        "stdin_tty": True,
        "stdout_tty": True,
        "term": "xterm-256color",
        "managed": False,
        "ambient_chat": "c98",
    }
    inputs.update(kwargs)

    result = resolve_browse_presentation(**inputs)  # type: ignore[arg-type]

    assert isinstance(result, expected_type)
    if isinstance(result, Refused):
        assert "session c98" in result.reason
