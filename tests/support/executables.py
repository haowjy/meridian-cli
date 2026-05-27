from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def prepend_fake_executables(monkeypatch: Any, tmp_path: Path, *names: str) -> None:
    """Put no-op executables on PATH for cross-platform preflight tests."""

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        unix_path = bin_dir / name
        unix_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        unix_path.chmod(0o755)
        cmd_path = bin_dir / f"{name}.cmd"
        cmd_path.write_text("@echo off\r\nexit /B 0\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
