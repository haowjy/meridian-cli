"""Scope sidecar decoding: malformed and missing fields are discarded, not guessed."""

from __future__ import annotations

import dataclasses

import pytest

from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state import process_scope_projection


def _scope(
    spawn_id: str,
    *,
    scope_id: str,
    owner_policy: str,
    owner_id: str,
    root_pid: int,
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
        owner_id=owner_id,
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=root_pid,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )


def _payload() -> dict[str, object]:
    return dataclasses.asdict(
        _scope(
            "p1",
            scope_id="backend",
            owner_policy="session_owned",
            owner_id="c1",
            root_pid=501,
        )
    )


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("scope_id", 1),
        ("owner_policy", 1),
        ("owner_id", 1),
        ("role", 1),
        ("containment", 1),
        ("root_pid", "501"),
        ("root_pid", True),
        ("root_created_at_epoch", "100.0"),
        ("root_created_at_epoch", True),
        ("pgid", "501"),
        ("pgid", True),
        ("job_name", 1),
        ("degraded_reason", 1),
        ("release_id", 1),
    ],
)
def test_malformed_scope_sidecar_field_is_discarded(
    field: str,
    malformed_value: object,
) -> None:
    payload = _payload()
    payload[field] = malformed_value

    assert process_scope_projection.scope_snapshot_from_dict(payload) is None


@pytest.mark.parametrize(
    "field",
    [
        "scope_id",
        "owner_policy",
        "owner_id",
        "role",
        "containment",
        "root_pid",
        "root_created_at_epoch",
        "pgid",
        "job_name",
        "degraded_reason",
    ],
)
def test_scope_sidecar_missing_required_field_is_discarded(field: str) -> None:
    payload = _payload()
    del payload[field]

    assert process_scope_projection.scope_snapshot_from_dict(payload) is None


def test_scope_sidecar_without_release_id_derives_stable_identity() -> None:
    snapshot = _scope(
        "p1",
        scope_id="backend",
        owner_policy="session_owned",
        owner_id="c1",
        root_pid=501,
    )
    payload = dataclasses.asdict(snapshot)
    del payload["release_id"]

    assert process_scope_projection.scope_snapshot_from_dict(payload) == snapshot
