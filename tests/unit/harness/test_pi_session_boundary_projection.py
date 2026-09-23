from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from meridian.lib.harness.projections.pi_extension_projection import (
    PiExtensionLaunchProfile,
    PiExtensionProjectionError,
    resolve_pi_extension_entrypoints,
)


def test_qualification_profile_selects_only_fresh_source_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "dist" / "extensions"
    source = tmp_path / "extensions" / "session-boundary" / "src" / "index.ts"
    shared_source = tmp_path / "extensions" / "shared" / "session_boundary.ts"
    bundle = root / "session-boundary" / "index.js"
    source.parent.mkdir(parents=True)
    shared_source.parent.mkdir(parents=True)
    bundle.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    shared_source.write_text("shared", encoding="utf-8")
    bundle.write_text("fresh", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(root))

    profile = PiExtensionLaunchProfile(False, False, False, session_boundary_enabled=True)
    assert resolve_pi_extension_entrypoints(profile) == (str(bundle.resolve()),)

    os.utime(source, ns=(time.time_ns() + 1_000_000_000, time.time_ns() + 1_000_000_000))
    with pytest.raises(PiExtensionProjectionError, match="stale"):
        resolve_pi_extension_entrypoints(profile)


def test_qualification_profile_does_not_use_installed_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "dist" / "extensions"
    root.mkdir(parents=True)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(root))
    profile = PiExtensionLaunchProfile(False, False, False, session_boundary_enabled=True)
    with pytest.raises(PiExtensionProjectionError, match="not accepted"):
        resolve_pi_extension_entrypoints(profile)
