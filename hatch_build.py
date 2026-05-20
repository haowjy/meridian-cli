"""Hatch build hooks for generated Meridian package assets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PI_RUNTIME_DIR = Path("src/meridian/pi_runtime")
PI_EXTENSION_ARTIFACTS = (
    PI_RUNTIME_DIR / "dist/extensions/managed-bash/index.js",
    PI_RUNTIME_DIR / "dist/extensions/meridian-lifecycle/index.js",
)


class CustomBuildHook(BuildHookInterface):
    """Build generated assets that must be present inside distributions."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        _build_pi_extensions_if_needed()


def _build_pi_extensions_if_needed() -> None:
    runtime_dir = Path.cwd() / PI_RUNTIME_DIR
    if not runtime_dir.exists():
        return

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        if _all_artifacts_present():
            # Building from an sdist should not require Node tooling; the sdist
            # carries the generated artifacts produced by the source build.
            return
        raise RuntimeError(
            "pnpm is required to build Meridian Pi extension artifacts. "
            "Install/enable pnpm with corepack, then rerun the build."
        )

    subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=runtime_dir, check=True)
    subprocess.run([pnpm, "run", "build:extensions"], cwd=runtime_dir, check=True)

    missing = [str(path) for path in PI_EXTENSION_ARTIFACTS if not (Path.cwd() / path).is_file()]
    if missing:
        raise RuntimeError(
            "Pi extension build completed but required artifacts are missing: "
            + ", ".join(missing)
        )


def _all_artifacts_present() -> bool:
    root = Path.cwd()
    return all((root / path).is_file() for path in PI_EXTENSION_ARTIFACTS)
