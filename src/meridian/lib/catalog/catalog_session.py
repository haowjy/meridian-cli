from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from meridian.lib.catalog.model_aliases import (
    AliasEntry,
    MarsResultCache,
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
        return self._project_root

    def resolve_model(self, name_or_alias: str) -> AliasEntry:
        return resolve_model(name_or_alias, self._project_root, cache=self._cache)

    def load_aliases(self, *, no_refresh_models: bool = False) -> list[AliasEntry]:
        return load_merged_aliases(
            self._project_root,
            cache=self._cache,
            no_refresh_models=no_refresh_models,
        )

    def alias_map(self, *, no_refresh_models: bool = False) -> dict[str, AliasEntry]:
        if no_refresh_models not in self._alias_map:
            by_alias: dict[str, AliasEntry] = {}
            for item in self.load_aliases(no_refresh_models=no_refresh_models):
                alias = item.alias.strip()
                if not alias:
                    continue
                by_alias[alias] = item
            self._alias_map[no_refresh_models] = by_alias
        return self._alias_map[no_refresh_models]


__all__ = ["CatalogSession"]
