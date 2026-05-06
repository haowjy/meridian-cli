"""Shared projection helpers for chat event normalizers."""

from __future__ import annotations

from typing import cast


def canonical_item_type(raw_type: str | None, name: str | None = None) -> str:
    """Map raw harness item/tool types to canonical chat item taxonomy."""

    value = f"{raw_type or ''} {name or ''}".lower()
    if any(token in value for token in ("exec", "shell", "command", "bash")):
        return "command_execution"
    if any(token in value for token in ("file", "patch", "edit", "write")):
        return "file_change"
    return raw_type or name or "tool_use"


def extract_files(payload: dict[str, object]) -> list[dict[str, object]]:
    """Extract normalized file records from common payload variants."""

    value = payload.get("files") or payload.get("paths")
    if isinstance(value, list):
        files: list[dict[str, object]] = []
        for entry in cast("list[object]", value):
            if isinstance(entry, str):
                files.append({"path": entry})
            elif isinstance(entry, dict):
                files.append(cast("dict[str, object]", entry))
        return files

    path = payload.get("path") or payload.get("file")
    if isinstance(path, str):
        result: dict[str, object] = {"path": path}
        for key in ("operation", "status"):
            if key in payload:
                result[key] = payload[key]
        return [result]

    properties = payload.get("properties")
    if isinstance(properties, dict):
        return extract_files(cast("dict[str, object]", properties))
    return []


def as_dict(value: object) -> dict[str, object] | None:
    """Return value as a dict when possible."""

    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def as_str(value: object) -> str | None:
    """Return value when it is a string."""

    return value if isinstance(value, str) else None


__all__ = ["as_dict", "as_str", "canonical_item_type", "extract_files"]
