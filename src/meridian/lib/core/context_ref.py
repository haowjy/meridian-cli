"""Resolved ``--from`` context payloads and their prompt rendering.

The spawn resolver (``ops.spawn.context_ref``) builds these at the request boundary;
launch carries and renders them without resolving anything itself.
"""

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import ACTIVE_SPAWN_STATUSES


class SpawnContextRef(BaseModel):
    """Resolved context payload sourced from one spawn."""

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
    work_goal: str | None = None
    task_cwd: str | None = None


class SessionContextRef(BaseModel):
    """Resolved context payload sourced from one chat/session."""

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
    work_goal: str | None = None
    task_cwd: str | None = None


type ContextRef = SpawnContextRef | SessionContextRef


def resolved_context_ref_value(ref: ContextRef) -> str:

    if ref.ref_kind == "session":
        return ref.chat_id
    return ref.spawn_id


def _message_line(target: str | None, status: str) -> list[str]:
    # Queued and finalizing spawns have no live control endpoint to take the message.
    if not target or not target.strip() or status != "running":
        return []
    return [f'- Still running; message it: `meridian spawn message {target} --message "..."`']


def _work_lines(ref: ContextRef) -> list[str]:
    if not ref.work_id:
        return []
    lines = [f"**Work item:** {ref.work_id}"]
    if ref.work_goal:
        # One line, so a goal can't add headings or close the block's tags.
        lines.append(f"**Work goal:** {' '.join(ref.work_goal.split())}")
    return lines


def _catch_up_lines(chat_id: str | None, work_id: str | None) -> list[str]:
    """Catch-up steps, cheapest first; commands only, never transcript text."""

    lines = ["", "## Catch up: this block does not say what that session discussed"]
    step = 1
    if work_id:
        lines.append(
            f"{step}. Recorded state first: `meridian work show {work_id}` names the work "
            "item's directory; read its `AGENTS.md`, plan and status there, and check "
            "open PRs."
        )
        step += 1
    if chat_id:
        lines.append(
            f"{step}. The end of the old session: `meridian session log {chat_id} "
            "--tail 20` (the owner's last messages and the replies)."
        )
        lines.append(
            f"{step + 1}. Then search, only for what is still missing: "
            f"`meridian session search <query> --ref {chat_id}`."
        )
    return lines if len(lines) > 2 else []


def _render_context_ref(ref: ContextRef) -> str:
    status = ref.status or "unknown"
    agent = ref.agent or "n/a"

    if ref.ref_kind == "session":
        tag_attrs = f'chat="{ref.chat_id}"'
        if ref.primary_spawn_id:
            tag_attrs += f' primary_spawn="{ref.primary_spawn_id}"'
        lines = [
            f"<prior-session-context {tag_attrs}>",
            f"# Session {ref.chat_id}",
            f"**Status:** {status} | **Agent:** {agent}",
        ]
        if ref.primary_spawn_id:
            lines[2] = f"**Primary spawn:** {ref.primary_spawn_id} | {lines[2]}"
        if ref.harness_session_id and ref.harness_session_id.strip():
            lines.append(
                f"**Harness:** {ref.harness or 'n/a'} | "
                f"**Harness session:** {ref.harness_session_id}"
            )
        lines.extend(_work_lines(ref))
        if ref.task_cwd:
            lines.append(f"**Task directory:** `{ref.task_cwd}`")
        lines.extend(_catch_up_lines(ref.chat_id, ref.work_id))
        lines.extend(["", "## Explore Further"])
        if ref.primary_spawn_id:
            lines.append(f"- Primary spawn: `meridian spawn show {ref.primary_spawn_id}`")
        lines.extend(_message_line(ref.chat_id if ref.primary_spawn_id else None, status))
        lines.append("</prior-session-context>")
        return "\n".join(lines)

    desc = ref.desc or "n/a"
    lines = [
        f'<prior-spawn-context spawn="{ref.spawn_id}">',
        f"# Spawn {ref.spawn_id}",
        f"**Status:** {status} | **Agent:** {agent} | **Desc:** {desc}",
    ]
    lines.extend(_work_lines(ref))
    if ref.task_cwd:
        lines.append(f"**Task directory:** `{ref.task_cwd}`")
    if ref.report_text and ref.report_text.strip():
        lines.extend(["", "## Report", ref.report_text.strip()])
    else:
        if status not in ACTIVE_SPAWN_STATUSES:
            lines.extend(["", "## Report", "No report available."])
        lines.extend(_catch_up_lines((ref.chat_id or "").strip() or None, ref.work_id))

    lines.append("")
    lines.append("## Explore Further")
    lines.append(f"- Full details: `meridian spawn show {ref.spawn_id}`")
    if ref.report_text and ref.report_text.strip() and ref.chat_id and ref.chat_id.strip():
        lines.append(f"- Session transcript: `meridian session log {ref.chat_id}`")
    lines.extend(_message_line(ref.spawn_id, status))
    lines.append("</prior-spawn-context>")
    return "\n".join(lines)


def render_context_refs(refs: Sequence[ContextRef]) -> str:

    if not refs:
        return ""
    return "\n\n".join(_render_context_ref(ref) for ref in refs)


def context_ref_values(refs: Sequence[ContextRef]) -> tuple[str, ...]:

    return tuple(resolved_context_ref_value(ref) for ref in refs)


__all__ = [
    "ContextRef",
    "SessionContextRef",
    "SpawnContextRef",
    "context_ref_values",
    "render_context_refs",
    "resolved_context_ref_value",
]
