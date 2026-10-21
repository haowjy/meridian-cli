"""Resolved ``--from`` context payloads and their prompt rendering.

The spawn resolver (``ops.spawn.context_ref``) builds these at the request boundary;
launch carries and renders them without resolving anything itself.
"""

import re
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SpawnContextRef(BaseModel):
    """Resolved context payload sourced from one prior spawn."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["spawn"] = "spawn"
    spawn_id: str
    status: str
    agent: str
    desc: str
    model: str
    harness: str
    report_text: str | None = None
    harness_session_id: str | None = None
    chat_id: str | None = None
    history_id: UUID | None = None
    work_id: str | None = None
    task_cwd: str | None = None


class SessionContextRef(BaseModel):
    """Resolved context payload sourced from one prior chat/session."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["session"] = "session"
    chat_id: str
    primary_spawn_id: str | None = None
    history_id: UUID | None = None
    status: str
    agent: str
    model: str
    harness: str
    harness_session_id: str | None = None
    work_id: str | None = None
    task_cwd: str | None = None


type ContextRef = SpawnContextRef | SessionContextRef

_SESSION_REF_RE = re.compile(r"^c\d+$")


def resolved_context_ref_value(ref: ContextRef) -> str:
    """Return the external resolved reference value for operation output."""

    if ref.ref_kind == "session":
        return ref.chat_id
    return ref.spawn_id


def _render_context_ref(ref: ContextRef) -> str:
    status = ref.status or "unknown"
    agent = ref.agent or "n/a"

    if ref.ref_kind == "session":
        tag_attrs = f'chat="{ref.chat_id}"'
        if ref.primary_spawn_id:
            tag_attrs += f' primary_spawn="{ref.primary_spawn_id}"'
        transcript_ref = (
            ref.chat_id
            if _SESSION_REF_RE.fullmatch(ref.chat_id) or ref.primary_spawn_id is None
            else ref.primary_spawn_id
        )
        lines = [
            f"<prior-session-context {tag_attrs}>",
            f"# Prior session: {ref.chat_id}",
            f"**Status:** {status} | **Agent:** {agent}",
        ]
        if ref.primary_spawn_id:
            lines[2] = f"**Primary spawn:** {ref.primary_spawn_id} | {lines[2]}"
        if ref.task_cwd:
            lines.append(f"**Task directory:** `{ref.task_cwd}`")
        lines.extend(
            [
                "",
                "## Explore Further",
                f"- Session transcript: `meridian session log {transcript_ref}`",
            ]
        )
        if ref.primary_spawn_id:
            lines.append(f"- Primary spawn: `meridian spawn show {ref.primary_spawn_id}`")
        if ref.harness_session_id and ref.harness_session_id.strip():
            lines.insert(
                3,
                f"**Harness:** {ref.harness or 'n/a'} | "
                f"**Harness session:** {ref.harness_session_id}",
            )
        lines.append("</prior-session-context>")
        return "\n".join(lines)

    desc = ref.desc or "n/a"
    lines = [
        f'<prior-spawn-context spawn="{ref.spawn_id}">',
        f"# Prior spawn: {ref.spawn_id}",
        f"**Status:** {status} | **Agent:** {agent} | **Desc:** {desc}",
    ]
    if ref.task_cwd:
        lines.append(f"**Task directory:** `{ref.task_cwd}`")
    lines.extend(
        [
            "",
            "## Report",
        ]
    )
    if ref.report_text and ref.report_text.strip():
        lines.append(ref.report_text.strip())
    else:
        lines.append("No report available.")

    lines.append("")
    lines.append("## Explore Further")
    lines.append(f"- Full details: `meridian spawn show {ref.spawn_id}`")
    if ref.chat_id and ref.chat_id.strip():
        lines.append(f"- Session transcript: `meridian session log {ref.chat_id}`")
    lines.append("</prior-spawn-context>")
    return "\n".join(lines)


def render_context_refs(refs: Sequence[ContextRef]) -> str:
    """Render resolved --from references as prior-context prompt blocks."""

    if not refs:
        return ""
    return "\n\n".join(_render_context_ref(ref) for ref in refs)


def context_ref_values(refs: Sequence[ContextRef]) -> tuple[str, ...]:
    """External resolved reference values, as recorded and reported."""

    return tuple(resolved_context_ref_value(ref) for ref in refs)


__all__ = [
    "ContextRef",
    "SessionContextRef",
    "SpawnContextRef",
    "context_ref_values",
    "render_context_refs",
    "resolved_context_ref_value",
]
