"""The portable record digest: SHA-256 over a record's JSON exactly as stored.

Integrity must not depend on the reader's models. A field added to a model
after a ZIP was written (``run_boundary``, ``native_store``) was never hashed by
that writer, so the digest is computed over the stored JSON object, never over
a re-serialization through today's models. Writers hash exactly what they store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pydantic import BaseModel

# Local/runtime-bound facts that do not identify portable content. Keys that a
# model no longer has stay listed so older stored records still project the same.
_LOCAL_STATE_FIELDS = frozenset(
    {
        "id",
        "chat_id",
        "owner_chat_id",
        "parent_id",
        "state_revision",
        "session_instance_id",
        "prompt",
        "worker_pid",
        "runner_pid",
        "runner_created_at_epoch",
        "control_root",
        "task_cwd",
        "execution_cwd",
        "claude_config_dir",
        "cancel_intent",
        "runner_exit",
        "launch_policy_snapshot",
        "originating_bash_id",
        "record_mode",
        "launch_mode",
        "harness_session_id",
        "resident_rearm_count",
    }
)
_LOCAL_SESSION_FIELDS = frozenset(
    {
        "chat_id",
        "spawn_id",
        "history_id",
        "session_instance_id",
        "forked_from_chat_id",
        "record_mode",
        "harness_session_id",
        "harness_session_ids",
        "control_root",
        "task_cwd",
        "execution_cwd",
        "claude_config_dir",
    }
)
_METADATA_MEMBERS = frozenset({"state.json", "record.json"})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Portable record {what} is not a JSON object")
    return cast("dict[str, object]", value)


def stored_record_digest(stored: Mapping[str, object]) -> str:
    """Digest of one stored record object (manifest entry or ``record.json``)."""
    state = _object(stored.get("state"), "state")
    session = stored.get("session")
    files = stored.get("files")
    if not isinstance(files, list):
        raise ValueError("Portable record files are not a JSON array")
    members = [_object(member, "file") for member in cast("list[object]", files)]
    return digest(
        canonical(
            {
                "state": {k: v for k, v in state.items() if k not in _LOCAL_STATE_FIELDS},
                "session": None
                if session is None
                else {
                    k: v
                    for k, v in _object(session, "session").items()
                    if k not in _LOCAL_SESSION_FIELDS
                },
                "files": [m for m in members if m.get("name") not in _METADATA_MEMBERS],
            }
        )
    )


def _stored(model: BaseModel) -> dict[str, Any]:
    # Exactly the JSON an ArchivedRecord stores for this nested model.
    return json.loads(model.model_dump_json())


def model_record_digest(
    state: BaseModel, files: Iterable[BaseModel], session: BaseModel | None
) -> str:
    """Digest of the record current models would store for these facts.

    New records are captured this way. Comparing a stored digest to this is only
    valid when both sides went through current models; use it for both sides.
    """
    return stored_record_digest(
        {
            "state": _stored(state),
            "session": None if session is None else _stored(session),
            "files": [_stored(member) for member in files],
        }
    )
