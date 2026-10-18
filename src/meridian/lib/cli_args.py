"""CLI grammar shared with code that reads Meridian command lines."""

from __future__ import annotations

# Keep in sync with `@app.default root(...)` in `cli/main.py` plus startup-only
# flags parsed before Cyclopts (`--verbose` at root).
TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "--format",
        "--config",
        "--continue",
        "--fork",
        "--fork-fresh",
        "--from",
        "--file",
        "-f",
        "--prompt-file",
        "--prompt",
        "-p",
        "--skills",
        "--goal",
        "--model",
        "-m",
        "--harness",
        "-a",
        "--agent",
        "--work",
        "--task-dir",
        "--autocompact",
        "--autocompact-pct",
        "--effort",
        "--sandbox",
        "--approval",
        "--timeout",
        "-C",
        "--directory",
        "--mode",
    }
)

__all__ = ["TOP_LEVEL_VALUE_FLAGS"]
