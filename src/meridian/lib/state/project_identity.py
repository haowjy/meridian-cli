"""The single resolution seam for committed and path-local project identity."""

from __future__ import annotations

import hashlib
import os
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.config.preserving_edit import (
    project_config_transaction,
    set_project_id,
)
from meridian.lib.config.project_root import has_project_marker
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.json_doc import write_model_once
from meridian.lib.state.user_paths import (
    get_project_home,
    get_user_home,
    project_lifetime_lock_path,
)
from meridian.lib.state.wordgen import generate_project_id

ADHOC_PROJECT_ID_PREFIX = "adhoc."
ADHOC_TOUCH_INTERVAL_SECONDS = 60 * 60
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Committed:
    """Identity anchored by a project marker."""

    id: str | None
    tier: Literal["committed"] = "committed"


@dataclass(frozen=True)
class AdHoc:
    """Identity derived from a canonical path and stored only in user state."""

    id: str
    materialized: bool
    tier: Literal["adhoc"] = "adhoc"


type ProjectIdentity = Committed | AdHoc


class AdHocOrigin(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    created_at: str


class _ProjectTable(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: object | None = None


class _CommittedConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    project: _ProjectTable | None = None


def state_id(identity: ProjectIdentity) -> str | None:
    """Return the persisted state ID visible through an identity."""

    if isinstance(identity, AdHoc) and not identity.materialized:
        return None
    return identity.id


def project_identity_was_created(
    before: ProjectIdentity,
    after: ProjectIdentity,
) -> bool:
    """Return whether an ensure operation first persisted an identity."""

    return (
        isinstance(before, Committed)
        and before.id is None
        and isinstance(after, Committed)
        and after.id is not None
    ) or (
        isinstance(before, AdHoc)
        and not before.materialized
        and isinstance(after, AdHoc)
        and after.materialized
    )


def get_committed_project_id(project_root: Path) -> str | None:
    """Read and validate the committed ID from ``meridian.toml``."""

    config_path = project_root / "meridian.toml"
    if not config_path.is_file():
        return None
    try:
        document = _CommittedConfig.model_validate(
            tomllib.loads(config_path.read_text(encoding="utf-8"))
        )
    except OSError:
        return None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{config_path.as_posix()}': {exc}") from exc
    except ValidationError as exc:
        raise ValueError(f"Invalid [project] table in '{config_path.as_posix()}'.") from exc
    if document.project is None:
        return None
    raw_id = document.project.id
    if raw_id is None:
        return None
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError(f"Invalid [project] id in '{config_path.as_posix()}'.")
    project_id = raw_id.strip()
    if project_id.startswith(ADHOC_PROJECT_ID_PREFIX):
        raise ValueError(
            f"Invalid [project] id in '{config_path.as_posix()}': "
            f"'{ADHOC_PROJECT_ID_PREFIX}' is reserved for path-local state."
        )
    return project_id


def _adhoc_project_id(project_root: Path) -> str:
    canonical = project_root.resolve()
    digest = hashlib.sha256(canonical.as_posix().encode("utf-8")).hexdigest()[:12]
    slug = _SLUG_PATTERN.sub("-", canonical.name.lower()).strip("-")[:24].rstrip("-")
    if not slug:
        slug = "root"
    return f"{ADHOC_PROJECT_ID_PREFIX}{slug}.{digest}"


def resolve_project_identity(project_root: Path) -> ProjectIdentity:
    """Resolve identity without creating or modifying any filesystem entry."""

    root = project_root.expanduser().resolve()
    if has_project_marker(root):
        return Committed(id=get_committed_project_id(root))

    project_id = _adhoc_project_id(root)
    return AdHoc(
        id=project_id,
        materialized=get_project_home(project_id).is_dir(),
    )


def nearest_ancestor_project_marker(project_root: Path) -> Path | None:
    """Return the nearest marked ancestor without changing identity resolution."""

    root = project_root.expanduser().resolve()
    return next((ancestor for ancestor in root.parents if has_project_marker(ancestor)), None)


def generate_committed_project_id(user_home: Path) -> str:
    """Generate an unused committed identity under one Meridian user home."""

    for _ in range(10):
        candidate = generate_project_id()
        if (
            not (user_home / "context" / candidate).exists()
            and not (user_home / "projects" / candidate).exists()
        ):
            return candidate
    raise RuntimeError("Failed to generate a unique project ID after 10 attempts")


def mint_committed_identity(
    project_root: Path,
    scaffold: str,
) -> Committed:
    """Atomically scaffold a project config with its committed identity."""

    project_root = project_root.expanduser().resolve()
    user_home = get_user_home()
    with project_config_transaction(project_root, user_home):
        existing = get_committed_project_id(project_root)
        if existing is not None:
            return Committed(id=existing)

        candidate = generate_committed_project_id(user_home)

        config_path = project_root / "meridian.toml"
        try:
            original = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            original = scaffold
        updated = set_project_id(original, candidate, config_path=config_path)
        if updated != original:
            atomic_write_text(config_path, updated)
        return Committed(id=candidate)


def _materialize_adhoc_identity(project_root: Path, identity: AdHoc) -> AdHoc:
    runtime_root = get_project_home(identity.id)
    lifetime_lock = project_lifetime_lock_path(runtime_root)
    origin_path = runtime_root / "origin.json"
    with lock_file(lifetime_lock, mode="shared", read_only_existing=True):
        if not origin_path.is_file():
            runtime_root.mkdir(parents=True, exist_ok=True)
            write_model_once(
                origin_path,
                AdHocOrigin(
                    path=project_root.as_posix(),
                    created_at=datetime.now(UTC).isoformat(),
                ),
            )
        else:
            if time.time() - origin_path.stat().st_mtime >= ADHOC_TOUCH_INTERVAL_SECONDS:
                os.utime(origin_path, None)
    return AdHoc(id=identity.id, materialized=True)


def ensure_project_identity(project_root: Path) -> ProjectIdentity:
    """Ensure identity for a write path without littering an ad-hoc directory."""

    root = project_root.expanduser().resolve()
    identity = resolve_project_identity(root)
    if isinstance(identity, Committed):
        if identity.id is not None:
            return identity
        return mint_committed_identity(root, "")
    return _materialize_adhoc_identity(root, identity)


__all__ = [
    "ADHOC_PROJECT_ID_PREFIX",
    "ADHOC_TOUCH_INTERVAL_SECONDS",
    "AdHoc",
    "AdHocOrigin",
    "Committed",
    "ProjectIdentity",
    "ensure_project_identity",
    "generate_committed_project_id",
    "get_committed_project_id",
    "mint_committed_identity",
    "nearest_ancestor_project_marker",
    "project_identity_was_created",
    "resolve_project_identity",
    "state_id",
]
