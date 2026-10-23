"""Meridian-owned Codex execpolicy projection."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.launch.launch_types import CodexRuntimeResolution
from meridian.lib.platform.atomic import atomic_write_text

MERIDIAN_RULES_FILE_CONTENT = (
    "# This file is owned and rewritten by Meridian.\n"
    'prefix_rule(pattern=["meridian", "spawn"], decision="allow")\n'
)


def ensure_codex_meridian_rules(
    launch_env: Mapping[str, str],
    *,
    child_cwd: Path,
) -> CodexRuntimeResolution:
    """Atomically project Meridian's rule into the launch environment's Codex home."""

    codex_home = resolve_codex_home(launch_env)
    if not codex_home.is_absolute():
        codex_home = child_cwd / codex_home
    rules_path = codex_home / "rules" / "meridian.rules"
    try:
        rules_path = codex_home.resolve() / "rules" / "meridian.rules"
        try:
            if rules_path.read_bytes() == MERIDIAN_RULES_FILE_CONTENT.encode():
                return CodexRuntimeResolution(rules_file_path=rules_path.as_posix())
        except FileNotFoundError:
            pass
        atomic_write_text(rules_path, MERIDIAN_RULES_FILE_CONTENT)
    except OSError as error:
        return CodexRuntimeResolution(
            rules_file_path=rules_path.as_posix(),
            rules_file_error=str(error),
        )
    return CodexRuntimeResolution(rules_file_path=rules_path.as_posix())


__all__ = ["MERIDIAN_RULES_FILE_CONTENT", "ensure_codex_meridian_rules"]
