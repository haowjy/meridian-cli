"""A spawn's permission-request journal, ``permission_requests.jsonl``.

One format for every request the model made that nobody could approve. ``PermissionBroker``
journals the requests Codex and OpenCode send to Meridian (opened, then refused); a harness
that refuses a call on its own reports ``PermissionDenial`` values at finalization, which
``PermissionJournal.record_denial`` writes in the same shape. ``spawn show`` reads the
refused ones back with ``denied_requests``.

An entry is a transition: the file keeps every ``pending``, ``resolved``, ``failed`` and
``cancelled`` row in order, and the current record of a request is its latest row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from meridian.lib.state.jsonl import append_durable_jsonl_line
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

PermissionRequestType = Literal["approval", "user_input"]
PermissionRequestStatus = Literal["pending", "resolved", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[PermissionRequestStatus] = frozenset(
    {"resolved", "failed", "cancelled"}
)

# The decision recorded for a request that was refused, whatever the harness calls it.
REJECT_DECISION = "reject"


@dataclass(frozen=True)
class PermissionDenial:
    """A call a harness refused under the profile's policy, as it reported it.

    ``request_id`` is the harness's own id for the call and makes recording idempotent;
    ``tool`` is the harness's name for the tool, ``detail`` what the call tried (command,
    path), ``reason`` why it was refused.
    """

    request_id: str
    tool: str
    detail: str
    reason: str


@dataclass(frozen=True)
class PermissionRequestRecord:
    """Current state snapshot for one permission/user-input request."""

    request_id: str
    request_type: PermissionRequestType
    method: str
    payload: dict[str, object]
    status: PermissionRequestStatus
    opened_seq: int
    updated_seq: int
    resolution: dict[str, object] | None = None
    error: str | None = None
    summary: str | None = None

    @property
    def denied(self) -> bool:
        return self.status == "resolved" and (self.resolution or {}).get("decision") == (
            REJECT_DECISION
        )

    @property
    def reason(self) -> str | None:
        """Why the request was refused, when the refusal said."""

        message = (self.resolution or {}).get("message")
        return message if isinstance(message, str) and message.strip() else None


@dataclass(frozen=True)
class PermissionTransition:
    """One durable transition entry from the permission request journal."""

    seq: int
    request_id: str
    request_type: PermissionRequestType
    method: str
    payload: dict[str, object]
    status: PermissionRequestStatus
    resolution: dict[str, object] | None
    error: str | None
    timestamp: str
    summary: str | None = None


class PermissionJournal:
    """The journal file and its current-state view.

    Loading tolerates a torn or unreadable row. Appends are not locked: the caller owns
    the file (the broker's lock while a spawn runs, finalization after it stopped).
    """

    def __init__(self, launch: LaunchPaths) -> None:
        self._runtime_root = launch.runtime_root
        self._spawn_id = launch.spawn_id
        self._path = launch.permission_requests
        self._transitions: list[PermissionTransition] = []
        self._requests: dict[str, PermissionRequestRecord] = {}
        self._next_seq = 0
        self._load()

    def get_request(self, request_id: str) -> PermissionRequestRecord | None:
        return self._requests.get(request_id)

    def list_requests(self) -> list[PermissionRequestRecord]:
        return sorted(self._requests.values(), key=lambda item: item.opened_seq)

    def list_transitions(self) -> list[PermissionTransition]:
        return list(self._transitions)

    def denied_requests(self) -> list[PermissionRequestRecord]:
        return [record for record in self.list_requests() if record.denied]

    def append(
        self,
        *,
        request_id: str,
        request_type: PermissionRequestType,
        method: str,
        payload: dict[str, object],
        status: PermissionRequestStatus,
        summary: str | None = None,
        resolution: dict[str, object] | None = None,
        error: str | None = None,
    ) -> PermissionTransition:
        transition = PermissionTransition(
            seq=self._next_seq,
            request_id=request_id,
            request_type=request_type,
            method=method,
            payload=dict(payload),
            status=status,
            resolution=dict(resolution) if resolution is not None else None,
            error=error,
            timestamp=_utc_now_iso(),
            summary=summary,
        )
        line = json.dumps(_transition_to_json(transition), sort_keys=True, separators=(",", ":"))
        mutate_published_spawn_artifact(
            self._runtime_root,
            self._spawn_id,
            lambda: append_durable_jsonl_line(self._path, line),
        )
        self._apply(transition)
        return transition

    def record_denial(self, denial: PermissionDenial) -> None:
        """Journal a call the harness refused; a call already journaled is left alone."""

        if denial.request_id in self._requests:
            return
        self.append(
            request_id=denial.request_id,
            request_type="approval",
            method=denial.tool,
            payload={"tool": denial.tool},
            status="resolved",
            summary=denial.detail,
            resolution={"decision": REJECT_DECISION, "message": denial.reason},
        )

    def _apply(self, transition: PermissionTransition) -> None:
        self._transitions.append(transition)
        self._next_seq = transition.seq + 1
        existing = self._requests.get(transition.request_id)
        opened_seq = transition.seq if existing is None else existing.opened_seq
        self._requests[transition.request_id] = PermissionRequestRecord(
            request_id=transition.request_id,
            request_type=transition.request_type,
            method=transition.method,
            payload=dict(transition.payload),
            status=transition.status,
            opened_seq=opened_seq,
            updated_seq=transition.seq,
            resolution=dict(transition.resolution) if transition.resolution is not None else None,
            error=transition.error,
            summary=transition.summary,
        )

    def _load(self) -> None:
        if not self._path.exists():
            return
        parsed = [
            transition
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if (transition := _parse_transition_line(line)) is not None
        ]
        for transition in sorted(parsed, key=lambda item: item.seq):
            self._apply(transition)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _transition_to_json(transition: PermissionTransition) -> dict[str, object]:
    return {
        "seq": transition.seq,
        "request_id": transition.request_id,
        "request_type": transition.request_type,
        "method": transition.method,
        "payload": transition.payload,
        "status": transition.status,
        "resolution": transition.resolution,
        "error": transition.error,
        "summary": transition.summary,
        "timestamp": transition.timestamp,
    }


def _parse_transition_line(line: str) -> PermissionTransition | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload_obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload_obj, dict):
        return None
    payload = cast("dict[str, object]", payload_obj)

    seq = payload.get("seq")
    request_id = payload.get("request_id")
    request_type = payload.get("request_type")
    method = payload.get("method")
    request_payload = payload.get("payload")
    status = payload.get("status")
    resolution = payload.get("resolution")
    error = payload.get("error")
    summary = payload.get("summary")
    timestamp = payload.get("timestamp")

    if not isinstance(seq, int):
        return None
    if not isinstance(request_id, str) or not request_id:
        return None
    if request_type not in ("approval", "user_input"):
        return None
    if not isinstance(method, str) or not method:
        return None
    if not isinstance(request_payload, dict):
        return None
    if status not in ("pending", "resolved", "failed", "cancelled"):
        return None
    if resolution is not None and not isinstance(resolution, dict):
        return None
    if error is not None and not isinstance(error, str):
        return None
    if summary is not None and not isinstance(summary, str):
        return None
    if not isinstance(timestamp, str) or not timestamp:
        return None

    typed_request_type: PermissionRequestType = request_type
    typed_status: PermissionRequestStatus = status
    return PermissionTransition(
        seq=seq,
        request_id=request_id,
        request_type=typed_request_type,
        method=method,
        payload=cast("dict[str, object]", request_payload),
        status=typed_status,
        resolution=cast("dict[str, object] | None", resolution),
        error=error,
        timestamp=timestamp,
        summary=summary,
    )


__all__ = [
    "REJECT_DECISION",
    "TERMINAL_STATUSES",
    "PermissionDenial",
    "PermissionJournal",
    "PermissionRequestRecord",
    "PermissionRequestStatus",
    "PermissionRequestType",
    "PermissionTransition",
]
