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

from meridian.lib.core.native_identity import NativeIdentityPlan


def read_header(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            header: object = json.loads(handle.readline())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"entry_mismatch: unreadable Pi header: {path}") from exc
    if (
        not isinstance(header, dict)
        or header.get("type") != "session"
        or not isinstance(header.get("id"), str)
        or not header["id"]
    ):
        raise ValueError(f"entry_mismatch: invalid Pi session header: {path}")
    return cast("dict[str, object]", header)


def mint_session_id(store: Path) -> str:
    session_id = str(uuid.uuid4())
    try:
        paths = tuple(store.iterdir())
    except FileNotFoundError:
        return session_id
    except OSError as exc:
        raise ValueError(f"native_identity_collision: cannot inspect {store}") from exc
    for path in paths:
        if path.suffix != ".jsonl":
            continue
        try:
            header = read_header(path)
        except ValueError as exc:
            raise ValueError(f"native_identity_collision: cannot verify {path}") from exc
        if header.get("id") == session_id:
            raise ValueError(f"native_identity_collision: {session_id} already exists in {path}")
    return session_id


def resolve_session_file(store: Path, session_id: str, *, pending: bool = False) -> Path | None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]", session_id) is None:
        raise ValueError("entry_mismatch: invalid Pi session ID")
    matches = list(store.glob(f"*_{session_id}.jsonl"))
    if len(matches) > 1:
        raise ValueError(f"ambiguous_native_file: {session_id} in {store}")
    if not matches:
        if pending:
            return None
        raise ValueError(f"native_transcript_missing: {session_id} in {store}")
    path = matches[0].absolute()
    if read_header(path).get("id") != session_id:
        raise ValueError(f"entry_mismatch: Pi header ID differs from {session_id}: {path}")
    return path


def verify_identity(plan: NativeIdentityPlan) -> Literal["ok", "pending"]:
    assert plan.native_store is not None and plan.harness_session_id is not None
    path = resolve_session_file(
        Path(plan.native_store),
        plan.harness_session_id,
        pending=plan.operation == "create",
    )
    if path is None:
        return "pending"
    if plan.operation == "resume" and str(path) != plan.locator:
        raise ValueError("entry_mismatch: Pi resume file changed")
    if plan.operation == "fork" and read_header(path).get("parentSession") != plan.locator:
        raise ValueError("entry_mismatch: Pi fork parentSession differs from source path")
    return "ok"


def project_identity(plan: NativeIdentityPlan | None, extra_args: tuple[str, ...]) -> list[str]:
    refused = {
        "--session",
        "-c",
        "--continue",
        "-r",
        "--resume",
        "--session-dir",
        "--session-id",
        "--fork",
        "--no-session",
    }
    for token in extra_args:
        if token.split("=", 1)[0] in refused:
            raise ValueError(f"Pi managed identity refuses {token} in passthrough extra_args")
    if plan is None:
        return []
    assert plan.native_store is not None and plan.harness_session_id is not None
    args = ["--session-dir", plan.native_store]
    if plan.operation == "resume":
        assert plan.locator is not None
        return [*args, "--session", plan.locator]
    if plan.operation == "fork":
        assert plan.locator is not None
        args.extend(("--fork", plan.locator))
    return [*args, "--session-id", plan.harness_session_id]
