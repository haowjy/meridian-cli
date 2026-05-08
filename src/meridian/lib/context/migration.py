"""Auto-migration for context backend paths."""

from pathlib import Path


def auto_migrate_contexts(runtime_root: Path) -> None:
    """Migrate old paths to the context-backend directory shape.

    Performs idempotent startup migration before context resolution:
    - ``.meridian/fs`` -> ``.meridian/kb``
    """

    fs_path = runtime_root / "fs"
    kb_path = runtime_root / "kb"
    if fs_path.exists() and not kb_path.exists():
        fs_path.rename(kb_path)

