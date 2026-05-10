"""Frontend asset resolution for the local chat server."""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FrontendAssets:
    """Resolved frontend asset location."""

    root: Path
    index_html: Path
    assets_dir: Path


def resolve_frontend_assets(
    *,
    explicit_dist: Path | None = None,
    project_root: Path | None = None,
) -> FrontendAssets | None:
    """Resolve built frontend assets.

    Resolution order:
    1. explicit_dist (--frontend-dist CLI override)
    2. Packaged assets (importlib.resources for meridian.web_dist)
    3. ../meridian-web/dist relative to project root (dev convenience)

    Returns None if no valid assets are found.
    """

    if explicit_dist is not None:
        return _validate_asset_dir(explicit_dist.expanduser().resolve())

    packaged_root = _packaged_asset_path()
    if packaged_root is not None:
        packaged_assets = _validate_asset_dir(packaged_root)
        if packaged_assets is not None:
            return packaged_assets

    sibling_root = _sibling_dist_path(project_root)
    if sibling_root is not None:
        sibling_assets = _validate_asset_dir(sibling_root)
        if sibling_assets is not None:
            return sibling_assets

    return None


def _validate_asset_dir(root: Path) -> FrontendAssets | None:
    index = root / "index.html"
    assets = root / "assets"
    if index.is_file() and assets.is_dir():
        return FrontendAssets(root=root, index_html=index, assets_dir=assets)
    return None


def _packaged_asset_path() -> Path | None:
    try:
        ref = importlib.resources.files("meridian.web_dist")
        root = Path(str(ref))
        if (root / "index.html").is_file():
            return root
    except (ModuleNotFoundError, TypeError):
        pass
    return None


def _sibling_dist_path(project_root: Path | None) -> Path | None:
    if project_root is None:
        return None
    try:
        sibling = project_root.parent / "meridian-web" / "dist"
        if sibling.is_dir():
            return sibling.resolve()
    except Exception:
        pass
    return None
