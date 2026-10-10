from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.harness.projections.pi_extension_projection import (
    PiExtensionLaunchProfile,
    resolve_pi_extension_entrypoints,
)

if TYPE_CHECKING:
    import pytest


def test_idle_bundle_is_interactive_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_root = tmp_path / "extensions"
    bundle_names = (
        "managed-bash",
        "meridian-spawn-watch",
        "session-boundary",
        "meridian-idle",
    )
    for name in bundle_names:
        entrypoint = extension_root / name / "index.js"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.touch()
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_root))

    interactive = resolve_pi_extension_entrypoints(
        PiExtensionLaunchProfile(
            background_tasks_enabled=True,
            spawn_watch_enabled=True,
            interactive=True,
        )
    )
    spawned = resolve_pi_extension_entrypoints(
        PiExtensionLaunchProfile(
            background_tasks_enabled=True,
            spawn_watch_enabled=True,
            interactive=False,
        )
    )

    assert tuple(Path(entrypoint).parent.name for entrypoint in interactive) == bundle_names
    assert tuple(Path(entrypoint).parent.name for entrypoint in spawned) == bundle_names[:3]
