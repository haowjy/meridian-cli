"""Readable and raw renderers for session-log output."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

from meridian.lib.harness.transcript import ToolCall

_CONTENT_PREVIEW_MAX_LINES = 80
_CONTENT_PREVIEW_MAX_CHARS = 8000
_TOOL_RESULT_HINT = "Use --no-truncate to expand tool outputs"
_TOOL_RESULT_PREFIX = "[tool_result]"  # Text marker prefix for tool results
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_COMMAND_BLOCK_RE = re.compile(
    r"(?:\s*<command-(?:name|args|message)>.*?</command-(?:name|args|message)>\s*)+",
    re.DOTALL,
)
_COMMAND_PART_RE = re.compile(
    r"<command-(?P<name>name|args|message)>(?P<body>.*?)</command-(?P=name)>",
    re.DOTALL,
)
_BASH_OUTPUT_BLOCK_RE = re.compile(
    r"(?:\s*<bash-(?:stdout|stderr)>.*?</bash-(?:stdout|stderr)>\s*)+",
    re.DOTALL,
)
_BASH_OUTPUT_PART_RE = re.compile(
    r"<bash-(?P<name>stdout|stderr)>(?P<body>.*?)</bash-(?P=name)>",
    re.DOTALL,
)
_NOTIFICATION_TAG_RE = re.compile(r"<(?P<name>status|summary)>(?P<body>.*?)</(?P=name)>", re.DOTALL)


class SessionLogRenderableMessage(Protocol):
    @property
    def segment_message(self) -> int: ...

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def tool_call(self) -> ToolCall | None: ...

    @property
    def is_tool_result(self) -> bool: ...


class SessionLogRenderableEntry(Protocol):
    @property
    def index(self) -> int: ...

    @property
    def segment(self) -> int: ...

    @property
    def segment_start_message(self) -> int: ...

    @property
    def segment_end_message(self) -> int: ...

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def messages(self) -> Sequence[SessionLogRenderableMessage]: ...


def _replace_tag(
    text: str,
    *,
    name: str,
    renderer: Callable[[str], str],
) -> str:
    pattern = re.compile(rf"<{name}>(.*?)</{name}>", re.DOTALL)

    def _replacement(match: re.Match[str]) -> str:
        return renderer(match.group(1))

    return pattern.sub(_replacement, text)


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _normalize_command_name(name: str) -> str:
    normalized = name.strip().lstrip("/")
    return f"/{normalized}" if normalized else "/command"


def _render_command_block(match: re.Match[str]) -> str:
    parts: dict[str, str] = {}
    for part in _COMMAND_PART_RE.finditer(match.group(0)):
        parts[part.group("name")] = part.group("body")

    raw_name = parts.get("name", "").strip()
    if not raw_name:
        fallback_parts = [parts.get("message", ""), parts.get("args", "")]
        fallback = " ".join(" ".join(part.split()) for part in fallback_parts if part.strip())
        return fallback

    name = _normalize_command_name(raw_name)
    args = " ".join(parts.get("args", "").split())
    command = name
    if args:
        command = f"{command} {args}"
    return command


def _replace_command_blocks(text: str) -> str:
    return _COMMAND_BLOCK_RE.sub(_render_command_block, text)


def _render_bash_output_block(match: re.Match[str]) -> str:
    outputs: list[str] = []
    saw_stderr = False
    saw_stdout = False

    for part in _BASH_OUTPUT_PART_RE.finditer(match.group(0)):
        tag_name = part.group("name")
        body = part.group("body").strip()
        if tag_name == "stdout":
            saw_stdout = True
            if body:
                outputs.append(body)
            continue
        saw_stderr = True
        if body:
            outputs.append(f"stderr: {body}")

    if outputs:
        return "\n".join(outputs)
    if saw_stdout and not saw_stderr:
        return "(no output)"
    return ""


def _replace_bash_output_blocks(text: str) -> str:
    def _replacement(match: re.Match[str]) -> str:
        rendered = _render_bash_output_block(match)
        if not rendered:
            return ""
        leading = match.group(0).split("<", 1)[0]
        if "\n" in leading:
            return f"\n{rendered}"
        return rendered

    return _BASH_OUTPUT_BLOCK_RE.sub(_replacement, text)


def _render_system_notification(body: str) -> str:
    details: dict[str, str] = {}
    for match in _NOTIFICATION_TAG_RE.finditer(body):
        details[match.group("name")] = " ".join(match.group("body").split())
    status = details.get("status", "")
    summary = details.get("summary", "")
    if status and summary:
        return f"[notification: {status} — {summary}]"
    if summary:
        return f"[notification: {summary}]"
    if status:
        return f"[notification: {status}]"
    fallback = " ".join(body.split())
    if not fallback:
        return ""
    return f"[notification: {fallback}]"


def clean_content(text: str) -> str:
    """Strip known harness wrappers while preserving unknown tags."""

    if not text:
        return text

    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"</bash-input>\s*(?=<bash-(?:stdout|stderr)>)", "</bash-input>\n", cleaned)
    cleaned = _replace_command_blocks(cleaned)
    cleaned = _replace_tag(cleaned, name="local-command-caveat", renderer=lambda _inner: "")
    cleaned = _replace_tag(cleaned, name="system-reminder", renderer=lambda _inner: "")
    cleaned = _replace_tag(cleaned, name="usage", renderer=lambda _inner: "")
    cleaned = _replace_tag(
        cleaned,
        name="local-command-stdout",
        renderer=lambda inner: _strip_ansi(inner).strip(),
    )
    cleaned = _replace_tag(
        cleaned,
        name="bash-input",
        renderer=lambda inner: f"$ {inner.strip()}" if inner.strip() else "$",
    )
    cleaned = _replace_bash_output_blocks(cleaned)
    cleaned = _replace_tag(
        cleaned,
        name="system_notification",
        renderer=_render_system_notification,
    )
    cleaned = _replace_tag(cleaned, name="user_query", renderer=lambda inner: inner.strip())
    cleaned = _replace_tag(cleaned, name="persisted-output", renderer=lambda inner: inner.strip())
    cleaned = _replace_tag(
        cleaned,
        name="tool_use_error",
        renderer=lambda inner: f"[error: {' '.join(inner.split())}]" if inner.strip() else "",
    )

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _truncate_preview(content: str) -> str:
    if not content:
        return content

    line_parts = content.splitlines(keepends=True)
    limited_by_lines = len(line_parts) > _CONTENT_PREVIEW_MAX_LINES
    preview = "".join(line_parts[:_CONTENT_PREVIEW_MAX_LINES]) if limited_by_lines else content
    if len(preview) > _CONTENT_PREVIEW_MAX_CHARS:
        preview = preview[:_CONTENT_PREVIEW_MAX_CHARS]

    omitted_chars = len(content) - len(preview)
    if omitted_chars <= 0:
        return content

    omitted_lines = len(line_parts) - _CONTENT_PREVIEW_MAX_LINES if limited_by_lines else 0
    line_label = "line" if omitted_lines == 1 else "lines"
    char_label = "char" if omitted_chars == 1 else "chars"
    marker = (
        f"...[truncated: omitted {omitted_lines} {line_label}, "
        f"{omitted_chars} {char_label}; rerun with --no-truncate]"
    )
    if not preview:
        return marker
    separator = "" if preview.endswith("\n") else "\n"
    return f"{preview}{separator}{marker}"


def _tool_result_body(content: str) -> str | None:
    """Extract tool result content from text marker (fallback for untyped messages)."""
    stripped = content.strip()
    if not stripped.startswith(_TOOL_RESULT_PREFIX):
        return None
    return stripped[len(_TOOL_RESULT_PREFIX) :].strip()


def _role_label(role: str) -> str:
    normalized = role.strip().lower()
    if normalized == "user":
        return "User"
    if normalized == "assistant":
        return "Assistant"
    if normalized == "system":
        return "System"
    return normalized.title() or "Message"


def _session_label(*, session_id: str, requested_ref: str | None) -> str:
    requested = (requested_ref or "").strip()
    if requested:
        return requested
    return session_id


def _render_raw_header(
    *,
    session_id: str,
    requested_ref: str | None,
    source: str | None,
    showing: str,
    total_entries: int,
) -> str:
    resolved_source = source or ""
    normalized_requested_ref = (requested_ref or "").strip()
    if normalized_requested_ref and normalized_requested_ref != session_id:
        session_label = (
            f"{normalized_requested_ref} ({resolved_source}: {session_id})"
            if resolved_source
            else f"{normalized_requested_ref} ({session_id})"
        )
    else:
        session_label = f"{session_id} ({resolved_source})" if resolved_source else session_id
    entry_label = "entry" if total_entries == 1 else "entries"
    return f"Session {session_label} — showing {showing} of {total_entries} {entry_label}"


def _render_clean_header(
    *,
    session_id: str,
    requested_ref: str | None,
    total_entries: int,
    segment_label: str | None,
    showing: str,
) -> tuple[str, str]:
    entry_label = "entry" if total_entries == 1 else "entries"
    segment_summary = segment_label or "all segments"
    return (
        f"# Session {_session_label(session_id=session_id, requested_ref=requested_ref)}",
        f"{total_entries} {entry_label}, {segment_summary} · showing {showing}",
    )


def _indent_block(content: str, *, prefix: str = "  ") -> list[str]:
    if not content:
        return [f"{prefix}(no output)"]
    return [f"{prefix}{line}" if line else prefix.rstrip() for line in content.splitlines()]


def _tool_line(tool_call: ToolCall) -> str:
    """Render a normalized tool call as a collapsed one-liner."""
    name = tool_call.name
    detail = " ".join(tool_call.body.split())

    if name == "bash":
        return f"  $ {detail or 'bash'}"

    if name == "stdin":
        return "  (stdin)"

    for verb in ("read", "write", "edit", "grep"):
        if name == verb:
            return f"  {verb.title()} {detail}" if detail else f"  {verb.title()}"

    if detail:
        return f"  {name}: {detail}"
    return f"  {name}"


def _tool_failed_reason(result_body: str) -> str | None:
    normalized = clean_content(result_body)
    exit_match = re.search(
        r"(?:exit(?:ed)?(?:\s+(?:with\s+)?(?:status|code))?|exit_code|\[exit_code\])\s*[:=]?\s*(\d+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if exit_match is not None:
        exit_code = int(exit_match.group(1))
        if exit_code != 0:
            return f"exit {exit_code}"
        return None
    if re.search(r"\bfailed\b", normalized, flags=re.IGNORECASE):
        return "failed"
    return None


def _render_collapsed_tools(
    messages: Sequence[SessionLogRenderableMessage],
) -> tuple[list[str], bool]:
    rendered: list[str] = []
    collapsed = False
    index = 0
    while index < len(messages):
        message = messages[index]

        if message.tool_call is not None:
            tc = message.tool_call
            result_body: str | None = None
            if index + 1 < len(messages) and messages[index + 1].is_tool_result:
                result_body = _tool_result_body(messages[index + 1].content)
            rendered.append(_tool_line(tc))
            collapsed = True
            if result_body is not None and tc.name == "bash":
                failure = _tool_failed_reason(result_body)
                if failure is not None:
                    if failure.startswith("exit"):
                        rendered.append(f"  (failed: {failure})")
                    else:
                        rendered.append("  (failed)")
                index += 2
                continue
            if result_body is not None:
                index += 2
                continue
            index += 1
            continue

        if message.is_tool_result:
            collapsed = True
            result_body = _tool_result_body(message.content)
            if result_body is not None:
                summary = clean_content(result_body).splitlines()
                first_line = summary[0].strip() if summary else ""
                rendered.append(f"  (tool output): {first_line or '(no output)'}")
            else:
                rendered.append("  (tool output): (no output)")
            index += 1
            continue

        cleaned = clean_content(message.content)
        if cleaned:
            cleaned = _truncate_preview(cleaned)
            rendered.append(cleaned)
        index += 1

    return rendered, collapsed


def _render_expanded_tools(messages: Sequence[SessionLogRenderableMessage]) -> list[str]:
    rendered: list[str] = []
    index = 0
    while index < len(messages):
        message = messages[index]

        if message.tool_call is not None:
            rendered.append(_tool_line(message.tool_call))
            if index + 1 < len(messages) and messages[index + 1].is_tool_result:
                result_body = _tool_result_body(messages[index + 1].content)
                if result_body is not None:
                    rendered.extend(_indent_block(clean_content(result_body)))
                    index += 2
                    continue
            index += 1
            continue

        if message.is_tool_result:
            result_body = _tool_result_body(message.content)
            rendered.append("  (tool output)")
            if result_body is not None:
                rendered.extend(_indent_block(clean_content(result_body)))
            index += 1
            continue

        cleaned = clean_content(message.content)
        if cleaned:
            rendered.append(cleaned)
        index += 1

    return rendered


def render_entry(
    entry: SessionLogRenderableEntry,
    *,
    clean: bool,
    truncate: bool,
) -> tuple[list[str], bool]:
    if not clean:
        lines = [
            f"--- {entry.index} [segment {entry.segment} · "
            f"messages {entry.segment_start_message}-{entry.segment_end_message}] "
            f"[{entry.role}] ---"
        ]
        if not entry.messages:
            content = _truncate_preview(entry.content) if truncate else entry.content
            lines.append(content)
            return lines, False
        if len(entry.messages) == 1:
            content = entry.messages[0].content
            lines.append(_truncate_preview(content) if truncate else content)
            return lines, False

        for message in entry.messages:
            lines.append("")
            lines.append(f"[message {message.segment_message} · {message.role}]")
            content = _truncate_preview(message.content) if truncate else message.content
            lines.append(content)
        return lines, False

    lines = ["---", "", f"**{_role_label(entry.role)}** [{entry.index}]", ""]
    if not entry.messages:
        content = clean_content(entry.content)
        if truncate:
            content = _truncate_preview(content)
        if content:
            lines.append(content)
        return lines, False

    collapsed = False
    if truncate:
        body_lines, collapsed = _render_collapsed_tools(entry.messages)
    else:
        body_lines = _render_expanded_tools(entry.messages)

    for body_line in body_lines:
        if body_line:
            lines.append(body_line)
    return lines, collapsed


def render_session_log(
    *,
    session_id: str,
    requested_ref: str | None,
    source: str | None,
    total_entries: int,
    segment_entries: int | None,
    segment_label: str | None,
    showing: str,
    entries: Sequence[SessionLogRenderableEntry],
    previous_command: str | None,
    next_command: str | None,
    previous_segment_command: str | None,
    next_segment_command: str | None,
    hints: Sequence[str],
    truncate: bool,
    verbosity: int,
) -> str:
    clean = verbosity <= 0
    lines: list[str] = []

    if clean:
        title, subtitle = _render_clean_header(
            session_id=session_id,
            requested_ref=requested_ref,
            total_entries=total_entries,
            segment_label=segment_label,
            showing=showing,
        )
        lines.extend([title, "", subtitle])
    else:
        lines.append(
            _render_raw_header(
                session_id=session_id,
                requested_ref=requested_ref,
                source=source,
                showing=showing,
                total_entries=total_entries,
            )
        )
        if segment_label is not None and segment_entries is not None:
            lines.append(f"{segment_label}; {segment_entries} entries in segment")

    collapsed_tool_output = False
    for entry in entries:
        lines.append("")
        entry_lines, entry_collapsed = render_entry(entry, clean=clean, truncate=truncate)
        collapsed_tool_output = collapsed_tool_output or entry_collapsed
        lines.extend(entry_lines)

    nav_lines: list[str] = []
    if previous_command is not None:
        nav_lines.append(f"Previous: {previous_command}")
    if next_command is not None:
        nav_lines.append(f"Next: {next_command}")
    if previous_segment_command is not None:
        nav_lines.append(f"Previous segment: {previous_segment_command}")
    if next_segment_command is not None:
        nav_lines.append(f"Next segment: {next_segment_command}")
    if nav_lines:
        lines.append("")
        lines.extend(nav_lines)

    rendered_hints = list(hints)
    if clean and truncate and collapsed_tool_output and _TOOL_RESULT_HINT not in rendered_hints:
        rendered_hints.append(_TOOL_RESULT_HINT)
    if rendered_hints:
        lines.append("")
        lines.extend(rendered_hints)

    return "\n".join(lines)


__all__ = ["clean_content", "render_entry", "render_session_log"]
