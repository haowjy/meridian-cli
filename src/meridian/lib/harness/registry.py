from typing import Self

from pydantic import BaseModel, ConfigDict, PrivateAttr

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import plugin_hosted
from meridian.lib.harness.adapter import HarnessContract, NativeStoreReader, SubprocessHarness
from meridian.lib.harness.bundle import get_connection_cls, get_harness_bundle

type HarnessEntry = SubprocessHarness


def _empty_adapters() -> dict[HarnessId, HarnessEntry]:
    return {}


def _empty_readers() -> dict[HarnessId, NativeStoreReader]:
    return {}


class HarnessRegistry(BaseModel):
    """Registry keyed by HarnessId."""

    model_config = ConfigDict()

    _adapters: dict[HarnessId, HarnessEntry] = PrivateAttr(default_factory=_empty_adapters)
    _readers: dict[HarnessId, NativeStoreReader] = PrivateAttr(default_factory=_empty_readers)

    @classmethod
    def with_defaults(cls) -> Self:
        # Load-bearing: registers bundles and runs the projection/accounting guards
        # before the direct adapter imports below.
        # Harness extension touchpoints are documented in
        # `meridian.lib.harness.HARNESS_EXTENSION_TOUCHPOINTS`.
        from meridian.lib.harness import ensure_bootstrap

        ensure_bootstrap()
        from meridian.lib.harness.claude import ClaudeAdapter
        from meridian.lib.harness.codex import CodexAdapter
        from meridian.lib.harness.opencode import OpenCodeAdapter
        from meridian.lib.harness.pi import PiAdapter

        registry = cls()
        registry.register(ClaudeAdapter())
        registry.register(CodexAdapter())
        registry.register(OpenCodeAdapter())
        registry.register(PiAdapter())
        for name, reader in plugin_hosted.readers().items():
            registry._readers[HarnessId(name)] = reader
        return registry

    def register(self, adapter: HarnessEntry) -> None:
        self._adapters[adapter.id] = adapter

    def get(self, harness_id: HarnessId) -> HarnessEntry:
        if harness_id not in self._adapters:
            raise KeyError(f"Unknown harness '{harness_id}'")
        return self._adapters[harness_id]

    def find(self, name: str | None) -> HarnessEntry | None:
        """The adapter for a harness name read from stored data; None when unknown."""
        try:
            return self._adapters.get(HarnessId(name or ""))
        except ValueError:
            return None

    def get_reader(self, harness_id: HarnessId) -> NativeStoreReader:
        """What reads a harness's native sessions, launched by Meridian or not."""
        reader = self._adapters.get(harness_id) or self._readers.get(harness_id)
        if reader is None:
            raise KeyError(f"Unknown harness '{harness_id}'")
        return reader

    def find_reader(self, name: str | None) -> NativeStoreReader | None:
        """``get_reader`` for a name read from stored data; None when unknown."""
        try:
            return self.get_reader(HarnessId(name or ""))
        except (ValueError, KeyError):
            return None

    def get_subprocess_harness(self, harness_id: HarnessId) -> SubprocessHarness:
        return self.get(harness_id)

    def get_contract(self, harness_id: HarnessId) -> HarnessContract:
        return self.get(harness_id).contract

    def contracts(self) -> dict[HarnessId, HarnessContract]:
        return {harness_id: adapter.contract for harness_id, adapter in self._adapters.items()}

    def ids(self) -> tuple[HarnessId, ...]:
        return tuple(sorted(self._adapters))


_default_registry: HarnessRegistry | None = None


def get_default_harness_registry() -> HarnessRegistry:

    global _default_registry
    if _default_registry is None:
        _default_registry = HarnessRegistry.with_defaults()
    return _default_registry


__all__ = [
    "HarnessEntry",
    "HarnessRegistry",
    "get_connection_cls",
    "get_default_harness_registry",
    "get_harness_bundle",
]
