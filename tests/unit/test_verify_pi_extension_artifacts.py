from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify-pi-extension-artifacts.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_pi_extension_artifacts", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_extension_source(root: Path, name: str) -> None:
    path = root / "src" / "meridian" / "pi_runtime" / "extensions" / name / "src" / "index.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("export default {};\n")


def write_local_artifact(root: Path, name: str) -> None:
    path = root / "src" / "meridian" / "pi_runtime" / "dist" / "extensions" / name / "index.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("export default {};\n")


def write_archives(root: Path, dist: Path, names: tuple[str, ...]) -> None:
    dist.mkdir()
    wheel = dist / "meridian_cli-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in names:
            archive.writestr(
                f"meridian/pi_runtime/dist/extensions/{name}/index.js",
                "export default {};\n",
            )

    sdist = dist / "meridian_cli-0.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in names:
            artifact = root / name / "index.js"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("export default {};\n")
            archive.add(
                artifact,
                arcname=(
                    "meridian_cli-0.0.0/src/meridian/pi_runtime/"
                    f"dist/extensions/{name}/index.js"
                ),
            )


def test_verifies_dynamic_extension_set(tmp_path: Path) -> None:
    script = load_script()
    for name in ("managed-bash", "meridian-spawn-watch", "future-extension"):
        write_extension_source(tmp_path, name)
        write_local_artifact(tmp_path, name)
    (tmp_path / "src" / "meridian" / "pi_runtime" / "extensions" / "shared").mkdir(
        parents=True
    )
    dist = tmp_path / "dist"
    write_archives(
        tmp_path,
        dist,
        ("managed-bash", "meridian-spawn-watch", "future-extension"),
    )

    script.verify_distributions(dist, tmp_path)


def test_missing_extension_artifact_fails(tmp_path: Path) -> None:
    script = load_script()
    for name in ("managed-bash", "meridian-spawn-watch"):
        write_extension_source(tmp_path, name)
        write_local_artifact(tmp_path, name)
    dist = tmp_path / "dist"
    write_archives(tmp_path, dist, ("managed-bash",))

    with pytest.raises(script.VerificationError, match="Missing: meridian-spawn-watch"):
        script.verify_distributions(dist, tmp_path)


def test_stale_extension_artifact_fails(tmp_path: Path) -> None:
    script = load_script()
    write_extension_source(tmp_path, "managed-bash")
    write_local_artifact(tmp_path, "managed-bash")
    write_local_artifact(tmp_path, "removed-extension")
    dist = tmp_path / "dist"
    write_archives(tmp_path, dist, ("managed-bash", "removed-extension"))

    with pytest.raises(script.VerificationError, match="Unexpected: removed-extension"):
        script.verify_distributions(dist, tmp_path)
