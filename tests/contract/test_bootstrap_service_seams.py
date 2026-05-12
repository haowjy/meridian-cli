from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.lib.bootstrap.services import (
    build_chat_entrypoint,
    build_extension_entrypoint,
    build_spawn_application_service_from_entrypoint,
    build_spawn_entrypoint,
    build_spawn_lifecycle_service_from_roots,
    prepare_for_runtime_write,
)
from meridian.lib.service_context import ApplicationEntryPoint


@pytest.mark.parametrize(
    ("builder_name", "builder"),
    [
        ("spawn", build_spawn_entrypoint),
        ("chat", build_chat_entrypoint),
        ("extension", build_extension_entrypoint),
    ],
)
def test_entrypoint_builders_preserve_prepared_authority_context(
    tmp_path: Path,
    builder_name: str,
    builder: Callable[..., ApplicationEntryPoint],
) -> None:
    project_root = tmp_path / f"{builder_name}-repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)

    entrypoint = builder(prepared)

    assert entrypoint.context.project_root == prepared.project_root
    assert entrypoint.context.runtime_root == prepared.runtime_root
    assert entrypoint.context.config == prepared.config
    assert entrypoint.context.authority == prepared.authority
    assert entrypoint.services.lifecycle is None


@pytest.mark.parametrize(
    "builder",
    [
        build_spawn_entrypoint,
        build_chat_entrypoint,
        build_extension_entrypoint,
    ],
)
def test_spawn_application_service_from_entrypoint_reuses_injected_lifecycle_authority(
    tmp_path: Path,
    builder: Callable[..., ApplicationEntryPoint],
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    assert prepared.runtime_root is not None

    lifecycle = build_spawn_lifecycle_service_from_roots(project_root, prepared.runtime_root)
    entrypoint = builder(prepared, lifecycle=lifecycle)

    service = build_spawn_application_service_from_entrypoint(entrypoint)

    assert service.runtime_root == prepared.runtime_root
    assert service.lifecycle is lifecycle
