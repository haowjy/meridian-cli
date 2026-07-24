"""Pi failure-output normalization."""

import logging


def _pi_failure_output_verbose() -> bool:
    """Whether Pi failure text should include JS stack traces (e.g. extension errors)."""
    return logging.getLogger().isEnabledFor(logging.INFO)


def _is_js_stack_trace_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("at "):
        return True
    if stripped.startswith("(") and "file:///" in stripped:
        return True
    return "file:///" in stripped and ("/index.js:" in stripped or ".ts:" in stripped)


def compact_pi_failure_output(message: str, *, verbose: bool | None = None) -> str:
    """Collapse Pi extension/JS stack noise for user-facing spawn failure output."""
    text = message.strip()
    if not text:
        return text
    show_verbose = _pi_failure_output_verbose() if verbose is None else verbose
    if show_verbose:
        return text

    lines = text.splitlines()
    has_stack = any(_is_js_stack_trace_line(line) for line in lines)
    first_line = lines[0].strip() if lines else ""
    is_extension_error = first_line.startswith("Extension ") and " error:" in first_line
    if not has_stack and not is_extension_error:
        return text

    compact: list[str] = []
    for line in lines:
        if _is_js_stack_trace_line(line):
            break
        stripped = line.strip()
        if stripped:
            compact.append(stripped)
    if not compact:
        return first_line or text
    return compact[0] if is_extension_error else "\n".join(compact)
