"""One-shot in-memory upgrades for spawn state written before schema v3."""

from __future__ import annotations

from typing import Any, cast

# Remove this module once pre-v3 spawn rows no longer exist in the wild.

_RUNNER_EXIT_FIELDS = {
    "runner_exit_code": "exit_code",
    "runner_exit_status": "status",
    "runner_exit_error": "error",
    "runner_exit_at": "exited_at",
}
_TERMINAL_FIELDS = {
    "exit_code": "exit_code",
    "finished_at": "finished_at",
    "published_at": "published_at",
    "duration_secs": "duration_secs",
    "total_cost_usd": "total_cost_usd",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "cost_is_estimate": "cost_is_estimate",
    "error": "error",
    "terminal_origin": "origin",
}
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}

# Field inventory from origin/main's StoredSpawnState writer, excluding the
# flat projections handled above. This explicit allowlist makes unknown legacy
# data fail closed instead of disappearing during reshaping.
_COMMON_FIELDS = {
    "v",
    "id",
    "chat_id",
    "owner_chat_id",
    "parent_id",
    "originating_bash_id",
    "model",
    "agent",
    "agent_path",
    "skills",
    "skill_paths",
    "harness",
    "kind",
    "desc",
    "work_id",
    "goal",
    "display_label",
    "harness_session_id",
    "control_root",
    "task_cwd",
    "execution_cwd",
    "claude_config_dir",
    "launch_mode",
    "worker_pid",
    "runner_pid",
    "runner_created_at_epoch",
    "resident_rearm_count",
    "status",
    "started_at",
    "last_attempt_exited_at",
    "last_attempt_exit_code",
    "cancel_intent",
    "prompt_length",
    "launch_policy_snapshot",
}
_FLAT_FIELDS = _COMMON_FIELDS | set(_RUNNER_EXIT_FIELDS) | set(_TERMINAL_FIELDS)
_NESTED_FIELDS = _COMMON_FIELDS | {"runner_exit", "terminal"}


class LegacySpawnStateUpgradeError(ValueError):
    """A legacy row violated a deterministic upgrade rule."""

    def __init__(self, rule: str, message: str, *, fields: tuple[str, ...] = ()) -> None:
        self.rule = rule
        self.fields = fields
        super().__init__(f"Legacy spawn upgrade rule {rule!r} failed: {message}")


def _reject_unknown(raw: dict[str, Any], allowed: set[str]) -> None:
    unknown = tuple(sorted(set(raw) - allowed))
    if unknown:
        raise LegacySpawnStateUpgradeError(
            "unknown_fields",
            f"unknown legacy fields cannot be dropped: {', '.join(unknown)}",
            fields=unknown,
        )


def _require_fields(raw: dict[str, Any], fields: set[str], projection: str) -> None:
    missing = tuple(sorted(fields - set(raw)))
    if missing:
        raise LegacySpawnStateUpgradeError(
            f"incomplete_{projection}",
            f"{projection} projection is missing fields: {', '.join(missing)}",
            fields=missing,
        )


def _upgrade_flat_runner_exit(raw: dict[str, Any]) -> dict[str, Any] | None:
    _require_fields(raw, set(_RUNNER_EXIT_FIELDS), "runner_exit")
    values = {target: raw[source] for source, target in _RUNNER_EXIT_FIELDS.items()}
    if all(value is None for value in values.values()):
        return None
    required = ("status", "exit_code", "exited_at")
    missing = tuple(field for field in required if values[field] is None)
    if missing:
        raise LegacySpawnStateUpgradeError(
            "partial_runner_exit",
            f"runner exit facts are present but required values are null: {', '.join(missing)}",
            fields=missing,
        )
    return values


def _upgrade_flat_terminal(raw: dict[str, Any]) -> dict[str, Any] | None:
    _require_fields(raw, set(_TERMINAL_FIELDS), "terminal")
    status = raw.get("status")
    values = {target: raw[source] for source, target in _TERMINAL_FIELDS.items()}
    if status not in _TERMINAL_STATUSES:
        baseline: dict[str, Any] = {field: None for field in values}
        baseline["cost_is_estimate"] = False
        present = tuple(field for field, value in values.items() if value != baseline[field])
        if present:
            raise LegacySpawnStateUpgradeError(
                "terminal_facts_on_non_terminal_status",
                f"terminal facts are present on non-terminal status {status!r}: "
                f"{', '.join(present)}",
                fields=present,
            )
        return None

    required = ("exit_code", "finished_at", "published_at", "origin")
    missing = tuple(field for field in required if values[field] is None)
    if missing:
        raise LegacySpawnStateUpgradeError(
            "partial_terminal",
            f"terminal status {status!r} is missing required facts: {', '.join(missing)}",
            fields=missing,
        )
    return values


def _upgrade_nested(raw: dict[str, Any]) -> dict[str, Any]:
    terminal = raw.get("terminal")
    if terminal is not None:
        if not isinstance(terminal, dict):
            raise LegacySpawnStateUpgradeError(
                "invalid_nested_terminal", "nested terminal facts must be an object"
            )
        terminal = cast("dict[str, Any]", terminal)
        nested_status = terminal.get("status")
        if nested_status != raw.get("status"):
            raise LegacySpawnStateUpgradeError(
                "terminal_status_conflict",
                f"nested terminal status {nested_status!r} and top-level status "
                f"{raw.get('status')!r} disagree",
                fields=("status", "terminal.status"),
            )

    _reject_unknown(raw, _NESTED_FIELDS)
    upgraded = dict(raw)
    upgraded["v"] = 3
    if terminal is not None:
        upgraded["terminal"] = {key: value for key, value in terminal.items() if key != "status"}
    return upgraded


def upgrade_legacy_spawn_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Mechanically reshape a known v2 spawn row into the strict v3 shape.

    This function does not reconcile conflicting facts or discard unknown data.
    Any row outside the two known legacy shapes raises a rule-specific error.
    """

    if raw.get("v", 2) != 2:
        raise LegacySpawnStateUpgradeError(
            "unsupported_version", f"expected legacy version 2, got {raw.get('v')!r}"
        )
    if "terminal" in raw or "runner_exit" in raw:
        return _upgrade_nested(raw)

    _reject_unknown(raw, _FLAT_FIELDS)
    runner_exit = _upgrade_flat_runner_exit(raw)
    terminal = _upgrade_flat_terminal(raw)
    upgraded = {
        key: value
        for key, value in raw.items()
        if key not in _RUNNER_EXIT_FIELDS and key not in _TERMINAL_FIELDS
    }
    upgraded.update(v=3, runner_exit=runner_exit, terminal=terminal)
    return upgraded


__all__ = ["LegacySpawnStateUpgradeError", "upgrade_legacy_spawn_state"]
