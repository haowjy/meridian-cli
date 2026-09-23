from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from meridian.lib.harness.projections.pi_extension_projection import (
    PiExtensionLaunchProfile,
    PiExtensionProjectionError,
    resolve_pi_extension_entrypoints,
    resolve_pi_session_boundary_artifact,
)

INPUTS = (
    "extensions/session-boundary/src/index.ts",
    "extensions/shared/session_boundary.ts",
    "package.json",
    "pnpm-lock.yaml",
    "scripts/write-session-boundary-manifest.mjs",
)
FLAGS = [
    "tsup", "--format", "esm", "--target", "node20", "--splitting", "false",
    "--external", "@earendil-works/pi-coding-agent", "--external", "@earendil-works/pi-tui",
]


def source_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    root = runtime / "dist" / "extensions"
    for relative in INPUTS:
        file = runtime / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"input:{relative}", encoding="utf-8")
    bundle = root / "session-boundary" / "index.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("verified observer", encoding="utf-8")
    input_hashes = {
        relative: hashlib.sha256((runtime / relative).read_bytes()).hexdigest()
        for relative in INPUTS
    }
    output_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    identity = {"schema": 1, "flags": FLAGS, "inputs": input_hashes, "output_sha256": output_hash}
    artifact_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    (bundle.parent / "artifact.json").write_text(
        json.dumps({**identity, "artifact_id": artifact_id}), encoding="utf-8"
    )
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(root))
    return runtime, bundle


def test_projection_verifies_manifest_and_exposes_artifact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle = source_build(tmp_path, monkeypatch)
    profile = PiExtensionLaunchProfile(False, False, False, session_boundary_enabled=True)
    assert resolve_pi_extension_entrypoints(profile) == (str(bundle.resolve()),)
    artifact = resolve_pi_session_boundary_artifact()
    assert artifact.entrypoint == bundle.resolve()
    assert len(artifact.artifact_id) == 64
    assert artifact.output_sha256 == hashlib.sha256(bundle.read_bytes()).hexdigest()


def test_projection_rejects_copied_fake_newer_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle = source_build(tmp_path, monkeypatch)
    bundle.write_text("old unrelated copied artifact", encoding="utf-8")
    future = time.time_ns() + 1_000_000_000
    os.utime(bundle, ns=(future, future))
    with pytest.raises(PiExtensionProjectionError, match="verification failed"):
        resolve_pi_session_boundary_artifact()


@pytest.mark.parametrize(
    "changed_file",
    [
        "extensions/shared/session_boundary.ts",
        "package.json",
        "pnpm-lock.yaml",
        "scripts/write-session-boundary-manifest.mjs",
    ],
)
def test_projection_rejects_changed_source_or_build_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_file: str
) -> None:
    runtime_root, _ = source_build(tmp_path, monkeypatch)
    (runtime_root / changed_file).write_text("changed build input", encoding="utf-8")
    with pytest.raises(PiExtensionProjectionError, match="verification failed"):
        resolve_pi_session_boundary_artifact()


def test_qualification_profile_does_not_use_installed_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "dist" / "extensions"
    root.mkdir(parents=True)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(root))
    profile = PiExtensionLaunchProfile(False, False, False, session_boundary_enabled=True)
    with pytest.raises(PiExtensionProjectionError, match="not accepted"):
        resolve_pi_extension_entrypoints(profile)
