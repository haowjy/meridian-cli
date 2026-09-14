"""Operation-scoped collaborator for model catalog lookups."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from meridian.lib.catalog.model_aliases import (
    AliasEntry,
    MarsResultCache,
    cached_mars_models_list_all,
)
from meridian.lib.catalog.models import load_merged_aliases, resolve_model


@dataclass(init=False)
class CatalogSession:
    """Operation-scoped catalog resolution state.

    Created once per launch operation and discarded at operation end. The
    session owns its mars result cache internally so callers depend on catalog
    operations, not cache plumbing.
    """

    _project_root: Path
    _cache: MarsResultCache = field(default_factory=MarsResultCache, init=False)
    _alias_map: dict[bool, dict[str, AliasEntry]]

    def __init__(self, project_root: Path, cache: MarsResultCache | None = None) -> None:
        self._project_root = project_root
        self._cache = cache or MarsResultCache()
        self._alias_map = {}

    @property
    def project_root(self) -> Path:
        """Project root used for catalog resolution."""
        return self._project_root

    def resolve_model(self, name_or_alias: str) -> AliasEntry:
        """Resolve alias to model entry, caching mars calls for this operation."""
        return resolve_model(name_or_alias, self._project_root, cache=self._cache)

    def load_aliases(self, *, no_refresh_models: bool = False) -> list[AliasEntry]:
        """Load model aliases, caching mars calls for this operation."""
        return load_merged_aliases(
            self._project_root,
            cache=self._cache,
            no_refresh_models=no_refresh_models,
        )

    def alias_map(self, *, no_refresh_models: bool = False) -> dict[str, AliasEntry]:
        """Return aliases indexed by alias name, memoized for this operation."""
        if no_refresh_models not in self._alias_map:
            by_alias: dict[str, AliasEntry] = {}
            for item in self.load_aliases(no_refresh_models=no_refresh_models):
                alias = item.alias.strip()
                if not alias:
                    continue
                by_alias[alias] = item
            self._alias_map[no_refresh_models] = by_alias
        return self._alias_map[no_refresh_models]

    def list_all_models(self) -> list[dict[str, object]] | None:
        """List all known models, caching mars calls for this operation."""
        return cached_mars_models_list_all(self._project_root, cache=self._cache)


__all__ = ["CatalogSession"]
