"""Shared Claude harness utilities.

Kept separate from claude.py and extractors/claude.py to avoid circular imports.
"""


def extract_session_id_from_args(args: tuple[str, ...]) -> str | None:
    """Extract --session-id value from CLI args, or return None."""
    for i, token in enumerate(args):
        if token == "--session-id" and i + 1 < len(args):
            value = args[i + 1].strip()
            if value:
                return value
        if token.startswith("--session-id="):
            value = token.partition("=")[2].strip()
            if value:
                return value
    return None
