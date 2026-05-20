"""Helpers for tests that need projected Pi extension entrypoints."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def configure_pi_extension_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    """Create minimal fake Pi extension artifacts and point projection at them.

    Most launch/projection boundary tests only need Meridian to materialize
    entrypoint paths; they do not execute the bundled JavaScript. Keep those
    tests independent of the npm build step that produces real dist artifacts.
    """

    source_root = tmp_path / "pi-extension-source"
    target_root = tmp_path / "pi-extension-target"
    for extension_name in ("managed-bash", "meridian-lifecycle"):
        entrypoint = source_root / extension_name / "index.js"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("export default {}\n", encoding="utf-8")

    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(target_root))
    return source_root, target_root
