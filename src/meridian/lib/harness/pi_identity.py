"""Pi native identity: header-only preflight and exact-file verification.

Meridian never writes a Pi journal. Filename suffixes locate an assigned ID;
headers authorize opening it. Neither cwd nor mtime is identity evidence.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Literal, cast

import structlog

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeIdentity,
    NativeKeyFields,
    NativeSessionUnavailable,
)

logger = structlog.get_logger(__name__)


def read_header(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            header: object = json.loads(handle.readline())
    except (OSError, UnicodeError, ValueError) as exc:
        raise NativeSessionUnavailable(str(path), "missing") from exc
    if not isinstance(header, dict):
        raise NativeSessionUnavailable(str(path), "missing")
    payload = cast("dict[str, object]", header)
    if (
        payload.get("type") != "session"
        or not isinstance(payload.get("id"), str)
        or not payload["id"]
    ):
        raise NativeSessionUnavailable(str(path), "missing")
    return payload


def mint_session_id(store: Path) -> str:
    session_id = str(uuid.uuid4())
    try:
        paths = tuple(store.iterdir())
    except FileNotFoundError:
        return session_id
    except OSError as exc:
        raise ValueError(f"native_identity_collision: cannot inspect {store}") from exc
    for path in paths:
        if not path.name.endswith(".jsonl"):
            continue
        try:
            header = read_header(path)
        except ValueError:
            # A torn sibling must not disable fresh UUID launches in the shared store.
            # Exact source reads still fail closed; this scan is not an ID reservation.
            logger.warning("pi_store_unreadable_header", path=str(path))
            continue
        if header.get("id") == session_id:
            raise ValueError(f"native_identity_collision: {session_id} already exists in {path}")
    return session_id


def resolve_session_file(store: Path, session_id: str, *, pending: bool = False) -> Path | None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]", session_id) is None:
        raise NativeSessionUnavailable(session_id, "missing")
    matches = list(store.glob(f"*_{session_id}.jsonl"))
    if len(matches) > 1:
        raise NativeSessionUnavailable(session_id, "ambiguous_native_file")
    if not matches:
        if pending:
            return None
        raise NativeSessionUnavailable(session_id, "missing")
    path = matches[0].absolute()
    observed = read_header(path).get("id")
    if observed != session_id:
        raise NativeEntryMismatch(
            NativeKeyFields("pi", str(store), session_id),
            NativeKeyFields("pi", str(store), str(observed)),
        )
    return path


def verify_identity(plan: NativeIdentity) -> Literal["ok", "pending"]:
    if plan.session_id is None:
        raise NativeSessionUnavailable("pi", "unbound")
    path = resolve_session_file(
        Path(plan.native_store),
        plan.session_id,
        pending=plan.operation == "create",
    )
    if path is None:
        return "pending"
    if plan.operation == "resume" and path != plan.source:
        raise NativeEntryMismatch(
            NativeKeyFields("pi", plan.native_store, plan.session_id),
            NativeKeyFields("pi", str(path.parent), plan.session_id),
            reason="source_changed",
            detail=f"expected source {plan.source!r}, observed {str(path)!r}",
        )
    if plan.operation == "fork" and read_header(path).get("parentSession") != str(plan.source):
        raise NativeEntryMismatch(
            NativeKeyFields("pi", plan.native_store, plan.session_id),
            NativeKeyFields("pi", str(path.parent), plan.session_id),
            reason="fork_parent",
            detail=(
                f"expected parent {plan.source!r}, "
                f"observed {read_header(path).get('parentSession')!r}"
            ),
        )
    return "ok"


def project_identity(plan: NativeIdentity | None) -> list[str]:
    if plan is None:
        return []
    if plan.session_id is None:
        raise NativeSessionUnavailable("pi", "unbound")
    args = ["--session-dir", plan.native_store]
    if plan.operation == "resume":
        return [*args, "--session", str(plan.source)]
    if plan.operation == "fork":
        args.extend(("--fork", str(plan.source)))
    return [*args, "--session-id", plan.session_id]
