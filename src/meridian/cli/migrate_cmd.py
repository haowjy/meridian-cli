"""Top-level `meridian migrate` command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from meridian.cli.invocation import emit

if TYPE_CHECKING:
    from cyclopts import App


def register_migrate_command(app: App) -> None:
    def migrate() -> None:
        """Move legacy repo-local identity into meridian.toml."""
        from meridian.cli.utils import require_established_project_root
        from meridian.lib.ops.migration import migrate_project_id

        project_root = require_established_project_root()
        result = migrate_project_id(project_root)
        if result.status == "migrated":
            emit(
                "Migration complete. Commit these project identity changes:\n"
                "  meridian.toml\n"
                "  delete .meridian/id\n"
                "  delete .meridian/.gitignore"
            )
        elif result.status == "not-needed":
            emit("No migration needed: project identity already uses meridian.toml")
        else:
            raise SystemExit(f"Migration blocked: {result.blocking_reason or 'Unknown reason'}")

    app.command(migrate, name="migrate")


__all__ = ["register_migrate_command"]
