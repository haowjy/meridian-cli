"""Top-level `meridian migrate` command."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cyclopts import App


Emitter = Callable[[Any], None]


def register_migrate_command(app: App, emit: Emitter) -> None:
    @app.command(name="migrate")
    def migrate(  # pyright: ignore[reportUnusedFunction]
    ) -> None:
        """Migrate project ID from UUID format to three-word format.

        Converts a UUID-based project ID (e.g. 550e8400-e29b-41d4-a716-446655440000)
        to a human-readable three-word ID (e.g. bright-falcon-harbor).

        Migration is refused when active spawns are running. Stop all active spawns
        before migrating.

        Exits 0 on success or when no migration is needed. Exits 1 when blocked.
        """
        from meridian.lib.config.project_root import resolve_project_root
        from meridian.lib.ops.migration import migrate_project_id

        project_root = resolve_project_root()
        result = migrate_project_id(project_root)

        if result.status == "migrated":
            lines = [f"Migrated project ID: {result.old_id} → {result.new_id}"]
            if result.moved_context:
                lines.append(f"  Moved context: ~/.meridian/context/{result.new_id}")
            if result.moved_runtime:
                lines.append(f"  Moved runtime: ~/.meridian/projects/{result.new_id}")
            emit("\n".join(lines))
        elif result.status == "not-needed":
            if result.old_id:
                emit(
                    f"No migration needed: project ID '{result.old_id}' is already in word format"
                )
            else:
                reason = result.blocking_reason or "No project ID found"
                emit(f"No migration needed: {reason}")
        else:
            # blocked
            reason = result.blocking_reason or "Unknown reason"
            raise SystemExit(f"Migration blocked: {reason}")


__all__ = ["register_migrate_command"]
